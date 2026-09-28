import oci
import os
import time
import datetime
import json
import urllib.request
import urllib.parse


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

# If set, this OLD boot volume is deleted at startup (before any launch attempt).
# The instance is always launched fresh from an image, which creates a brand-new
# boot volume automatically. Leave unset/empty to skip deletion.
OLD_BOOT_VOLUME_ID = os.environ.get("OCI_BOOT_VOLUME_ID", "").strip()

# --- Grab-small-first, then upgrade strategy ---
# We first grab a SMALL shape (1 OCPU / 6 GB) because it has a much higher hit
# rate — the scheduler needs less contiguous free capacity. Once we have the
# instance, we try to resize it UP to the target (2 OCPU / 12 GB).
OCPUS = int(os.environ.get("OCI_OCPUS", "1"))
MEMORY_IN_GBS = int(os.environ.get("OCI_MEMORY_GBS", "6"))

# Upgrade target after a successful grab. Set either to 0 (or leave blank) to
# disable auto-upgrade and just keep the small instance.
RESIZE_TARGET_OCPUS = int(os.environ.get("OCI_RESIZE_TARGET_OCPUS", "2") or "0")
RESIZE_TARGET_MEMORY_GBS = int(os.environ.get("OCI_RESIZE_TARGET_MEMORY_GBS", "12") or "0")

RETRY_INTERVAL = int(os.environ.get("OCI_RETRY_INTERVAL", "90"))  # Seconds
# After a 429 (TooManyRequests), cool down for this long before retrying.
TOO_MANY_REQUESTS_WAIT = int(os.environ.get("OCI_TOO_MANY_REQUESTS_WAIT", "600"))

# --- Telegram notifications (optional) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# --------------------------------------------------------

if not COMPARTMENT_ID:
    raise SystemExit(
        "OCI_COMPARTMENT_ID is not set. Export it (and OCI_SSH_PUBLIC_KEY) "
        "before running, e.g. `export OCI_COMPARTMENT_ID=ocid1.tenancy...`"
    )

config = oci.config.from_file()


def send_telegram(message):
    """Send a Telegram message. No-op if token/chat id aren't configured.
    Never raises — a notification failure must not break the grab loop."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"⚠️ Telegram notify failed: {type(e).__name__}: {e}")


def get_instance_public_ip(compute, network, instance_id):
    """Best-effort lookup of an instance's public IP. Returns '' if not ready."""
    try:
        vnic_attachments = compute.list_vnic_attachments(
            COMPARTMENT_ID, instance_id=instance_id
        ).data
        for va in vnic_attachments:
            if va.vnic_id:
                vnic = network.get_vnic(va.vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
    except Exception:
        pass
    return ""


def get_availability_domain():
    identity = oci.identity.IdentityClient(config)
    ads = identity.list_availability_domains(COMPARTMENT_ID).data
    return ads[0].name


def delete_old_boot_volume():
    """Delete the OLD boot volume specified by OCI_BOOT_VOLUME_ID.

    OCI will not delete a boot volume that is still ATTACHED to an instance, so
    if the old instance still exists this will fail with a conflict — we report
    it clearly instead of crashing. Deletion is permanent and irreversible.
    """
    if not OLD_BOOT_VOLUME_ID:
        return

    blockstorage = oci.core.BlockstorageClient(config)

    try:
        bv = blockstorage.get_boot_volume(OLD_BOOT_VOLUME_ID).data
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            print(f"Old boot volume not found (already deleted?): {OLD_BOOT_VOLUME_ID}")
            return
        print(f"⚠️ Could not read old boot volume {OLD_BOOT_VOLUME_ID}: {e.message}")
        return

    if bv.lifecycle_state in ("TERMINATED", "TERMINATING"):
        print(f"Old boot volume already {bv.lifecycle_state}: {OLD_BOOT_VOLUME_ID}")
        return

    print(f"Deleting old boot volume: {OLD_BOOT_VOLUME_ID} (state={bv.lifecycle_state})")
    try:
        blockstorage.delete_boot_volume(OLD_BOOT_VOLUME_ID)
        # Wait until it's fully terminated so it stops counting against quotas.
        oci.wait_until(
            blockstorage,
            blockstorage.get_boot_volume(OLD_BOOT_VOLUME_ID),
            "lifecycle_state",
            "TERMINATED",
            max_wait_seconds=300,
        )
        print("✅ Old boot volume deleted.")
    except oci.exceptions.ServiceError as e:
        if e.status == 409:
            print(
                "❌ Cannot delete old boot volume: it is still attached to an "
                "instance. Terminate the old instance first, then re-run."
            )
        else:
            print(f"❌ Failed to delete old boot volume: {e.message}")
    except Exception as e:
        print(f"⚠️ Error while waiting for boot volume deletion: {type(e).__name__}: {e}")


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

    # Always launch from the image, which provisions a brand-new boot volume.
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


def upgrade_instance(compute, instance_id):
    """Resize the instance up to the target shape (RESIZE_TARGET_*).

    Returns (ok: bool, detail: str). The instance must be RUNNING before a
    shape update is accepted, so we wait for that first. A failed upgrade leaves
    the smaller instance fully usable.
    """
    if not (RESIZE_TARGET_OCPUS and RESIZE_TARGET_MEMORY_GBS):
        return False, "auto-upgrade disabled"

    try:
        print("   Waiting for instance to reach RUNNING before upgrade...")
        oci.wait_until(
            compute,
            compute.get_instance(instance_id),
            "lifecycle_state",
            "RUNNING",
            max_wait_seconds=600,
        )
    except Exception as e:
        return False, f"instance did not reach RUNNING: {type(e).__name__}: {e}"

    try:
        print(
            f"   Upgrading to {RESIZE_TARGET_OCPUS} OCPU / {RESIZE_TARGET_MEMORY_GBS} GB..."
        )
        compute.update_instance(
            instance_id,
            oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=RESIZE_TARGET_OCPUS,
                    memory_in_gbs=RESIZE_TARGET_MEMORY_GBS,
                )
            ),
        )
        return True, f"{RESIZE_TARGET_OCPUS} OCPU / {RESIZE_TARGET_MEMORY_GBS} GB"
    except oci.exceptions.ServiceError as e:
        return False, e.message or str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main():
    # Delete the old boot volume up front, before any launch attempt.
    delete_old_boot_volume()

    print("Initializing network settings...")
    subnet_id = create_vcn_and_subnet()

    print("Fetching Availability Domains...")
    ad_name = get_availability_domain()
    print(f"AD: {ad_name}")

    print("Fetching Ubuntu 24.04 Minimal aarch64 image...")
    image_id = get_ubuntu_image()
    print(f"Image ID: {image_id}")

    print(
        f"Strategy: grab {OCPUS} OCPU / {MEMORY_IN_GBS} GB first"
        + (
            f", then upgrade to {RESIZE_TARGET_OCPUS} OCPU / {RESIZE_TARGET_MEMORY_GBS} GB"
            if RESIZE_TARGET_OCPUS and RESIZE_TARGET_MEMORY_GBS
            else " (auto-upgrade disabled)"
        )
    )

    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)

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

            public_ip = get_instance_public_ip(compute, network, instance.id)

            send_telegram(
                "🎉 Grabbed ARM instance!\n"
                "─────────────────\n"
                f"📌 Name: {instance.display_name}\n"
                f"🆔 OCID: {instance.id}\n"
                f"📐 Shape: VM.Standard.A1.Flex ({OCPUS} OCPU / {MEMORY_IN_GBS} GB)\n"
                f"📍 AD: {ad_name}\n"
                f"🟢 State: {instance.lifecycle_state}\n"
                f"🌐 Public IP: {public_ip or '(pending — check console)'}\n"
                + (f"🔑 SSH: ssh ubuntu@{public_ip}\n" if public_ip else "")
            )

            # Now try to upgrade to the target shape.
            ok, detail = upgrade_instance(compute, instance.id)
            if ok:
                new_ip = get_instance_public_ip(compute, network, instance.id) or public_ip
                print(f"\n⬆️  Upgraded to {detail}")
                send_telegram(
                    "⬆️ Instance upgraded!\n"
                    "─────────────────\n"
                    f"📌 Name: {instance.display_name}\n"
                    f"📐 New shape: {detail}\n"
                    f"🌐 Public IP: {new_ip or '(check console)'}\n"
                    + (f"🔑 SSH: ssh ubuntu@{new_ip}\n" if new_ip else "")
                )
            else:
                print(f"\n⚠️ Upgrade skipped/failed: {detail}")
                if RESIZE_TARGET_OCPUS and RESIZE_TARGET_MEMORY_GBS:
                    send_telegram(
                        f"⚠️ Instance grabbed ({OCPUS} OCPU / {MEMORY_IN_GBS} GB), "
                        f"but upgrade to {RESIZE_TARGET_OCPUS}/{RESIZE_TARGET_MEMORY_GBS} "
                        f"failed:\n{detail}\n\n"
                        "The instance is usable; you can retry the upgrade later."
                    )
            break
        except oci.exceptions.ServiceError as e:
            # 429 TooManyRequests: back off hard, hammering makes it worse.
            if e.status == 429 or "TooManyRequests" in str(e):
                print(
                    f"🚦 429 TooManyRequests — cooling down for {TOO_MANY_REQUESTS_WAIT}s..."
                )
                time.sleep(TOO_MANY_REQUESTS_WAIT)
                continue
            if "Out of host capacity" in str(e) or "capacity" in str(e).lower():
                print(f"❌ Out of capacity, retrying...")
            else:
                print(f"❌ API Error: {e.message}, retrying...")
        except Exception as e:
            print(f"⚠️ Network timeout or other error, retrying... ({type(e).__name__})")

        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
