import oci
import os
import time
import datetime


def _load_dotenv(path=".env"):
    """Minimal .env loader (no external dependency). Values already present in
    the environment take precedence, so CI-provided secrets are never overridden."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# --- Settings ---
# Account-specific values are read from environment variables so no OCIDs or
# keys need to live in this (public) source file. For local runs you can either
# export these env vars or leave them unset and rely on a local override.
COMPARTMENT_ID = os.environ.get("OCI_COMPARTMENT_ID", "").strip()

def _clean_ssh_key(raw):
    """Normalize an SSH public key that may have been mangled by copy/paste or
    a secrets store: strip surrounding whitespace and collapse any internal
    newlines/extra spaces so it becomes a single valid `<type> <base64> [comment]`
    line. Also strip surrounding quotes if present."""
    if not raw:
        return ""
    raw = raw.strip().strip('"').strip("'")
    # Collapse ALL whitespace (newlines, tabs, multiple spaces) into single spaces.
    return " ".join(raw.split())


SSH_PUBLIC_KEY = _clean_ssh_key(os.environ.get("OCI_SSH_PUBLIC_KEY", ""))

# If set, the instance is launched from this existing boot volume instead of
# creating a fresh one from an image. Leave unset/empty to use the image flow.
BOOT_VOLUME_ID = os.environ.get("OCI_BOOT_VOLUME_ID", "").strip()

# Instance shape sizing (defaults to the common Always Free ceiling of 2/12).
OCPUS = int(os.environ.get("OCI_OCPUS", "2"))
MEMORY_IN_GBS = int(os.environ.get("OCI_MEMORY_GBS", "12"))

RETRY_INTERVAL = int(os.environ.get("OCI_RETRY_INTERVAL", "90"))  # Seconds
# --------------------------------------------------------

if not COMPARTMENT_ID:
    raise SystemExit(
        "OCI_COMPARTMENT_ID is not set. Export it (and OCI_SSH_PUBLIC_KEY) "
        "before running, e.g. `export OCI_COMPARTMENT_ID=ocid1.tenancy...`"
    )

config = oci.config.from_file()


def get_availability_domain():
    # When launching from an existing boot volume, the instance MUST be in the
    # same AD as that volume. Read the volume's AD directly so we don't guess.
    if BOOT_VOLUME_ID:
        blockstorage = oci.core.BlockstorageClient(config)
        bv = blockstorage.get_boot_volume(BOOT_VOLUME_ID).data
        return bv.availability_domain

    identity = oci.identity.IdentityClient(config)
    ads = identity.list_availability_domains(COMPARTMENT_ID).data
    return ads[0].name


def get_ubuntu_image():
    compute = oci.core.ComputeClient(config)
    # Search for Ubuntu 24.04 Minimal aarch64
    images = compute.list_images(
        COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04 Minimal aarch64",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        # Fallback search if the specific version string is slightly different in API
        images = compute.list_images(
            COMPARTMENT_ID,
            operating_system="Canonical Ubuntu",
            shape="VM.Standard.A1.Flex",
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        # Try to find the most relevant one manually
        for img in images:
            if "24.04" in img.display_name and "Minimal" in img.display_name:
                return img.id
        raise Exception("Ubuntu 24.04 Minimal ARM image not found")
    return images[0].id


def create_vcn_and_subnet():
    network = oci.core.VirtualNetworkClient(config)

    # Check if VCN already exists
    vcns = network.list_vcns(COMPARTMENT_ID, display_name="retry-vcn").data
    if vcns:
        vcn = vcns[0]
        print(f"Using existing VCN: {vcn.id}")
    else:
        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=COMPARTMENT_ID,
                display_name="retry-vcn",
                cidr_block="10.0.0.0/16",
            )
        ).data
        print(f"Created VCN: {vcn.id}")

        # Create Internet Gateway
        ig = network.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-ig",
                is_enabled=True,
            )
        ).data

        # Configure route table (allow outbound traffic)
        network.update_route_table(
            vcn.default_route_table_id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=[
                    oci.core.models.RouteRule(
                        destination="0.0.0.0/0",
                        network_entity_id=ig.id,
                    )
                ]
            ),
        )

        # Open inbound SSH / HTTP / HTTPS / Streamlit
        security_lists = network.list_security_lists(COMPARTMENT_ID, vcn_id=vcn.id).data
        if security_lists:
            existing_egress = security_lists[0].egress_security_rules
            new_ingress = []
            for port in [22, 80, 443, 8501]:
                new_ingress.append(
                    oci.core.models.IngressSecurityRule(
                        protocol="6",
                        source="0.0.0.0/0",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=port, max=port
                            )
                        ),
                    )
                )
            network.update_security_list(
                security_lists[0].id,
                oci.core.models.UpdateSecurityListDetails(
                    ingress_security_rules=new_ingress,
                    egress_security_rules=existing_egress,
                ),
            )

    # Check if subnet already exists
    subnets = network.list_subnets(
        COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-subnet"
    ).data
    if subnets:
        subnet = subnets[0]
        print(f"Using existing subnet: {subnet.id}")
    else:
        subnet = network.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-subnet",
                cidr_block="10.0.0.0/24",
                prohibit_public_ip_on_vnic=False,
            )
        ).data
        print(f"Created subnet: {subnet.id}")

    return subnet.id


def try_create_instance(subnet_id, ad_name, image_id):
    compute = oci.core.ComputeClient(config)

    if BOOT_VOLUME_ID:
        # Reuse an existing boot volume: OS and disk size come from the volume.
        source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(
            boot_volume_id=BOOT_VOLUME_ID,
        )
    else:
        source_details = oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=200,
        )

    instance = compute.launch_instance(
        oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            display_name="ubuntu-24-minimal",
            availability_domain=ad_name,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=OCPUS,
                memory_in_gbs=MEMORY_IN_GBS,
            ),
            source_details=source_details,
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
        )
    ).data
    return instance


def main():
    print("Initializing network settings...")
    subnet_id = create_vcn_and_subnet()

    print("Fetching Availability Domains...")
    ad_name = get_availability_domain()
    print(f"AD: {ad_name}")

    if BOOT_VOLUME_ID:
        print(f"Using existing boot volume: {BOOT_VOLUME_ID}")
        image_id = None
    else:
        print("Fetching Ubuntu 24.04 Minimal aarch64 image...")
        image_id = get_ubuntu_image()
        print(f"Image ID: {image_id}")

    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] Attempt {attempt} to create instance...")

        try:
            instance = try_create_instance(subnet_id, ad_name, image_id)
            print(f"\n✅ Success! Instance created")
            print(f"   ID: {instance.id}")
            print(f"   Status: {instance.lifecycle_state}")
            print(f"   Please check the Oracle Cloud Console for the public IP")
            break
        except oci.exceptions.ServiceError as e:
            if "Out of host capacity" in str(e) or "capacity" in str(e).lower():
                print(f"❌ Out of capacity, retrying...")
            else:
                print(f"❌ API Error: {e.message}, retrying...")
        except Exception as e:
            print(f"⚠️ Network timeout or other error, retrying... ({type(e).__name__})")

        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
