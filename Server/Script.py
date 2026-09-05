import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

import oci


# ============================================================
# OCI Always Free capacity monitor / provisioner
# Designed for a home region such as Singapore West:
#   ap-singapore-2
# and a tenancy with a single availability domain.
#
# The script NEVER puts credentials in source code and NEVER
# prints secret values. It retries only genuine host-capacity
# errors. Other errors stop the run so they can be fixed.
# ============================================================

DISPLAY_NAME = os.getenv("OCI_INSTANCE_NAME", "FX-Backend-Server")
VNIC_NAME = os.getenv("OCI_VNIC_NAME", "fx-backend-vnic")

# Approximately 5h45m. This stays below GitHub's current 6-hour
# GitHub-hosted job limit and leaves a safety margin.
RUN_SECONDS = int(os.getenv("OCI_RUN_SECONDS", str(5 * 60 * 60 + 45 * 60)))
RETRY_INTERVAL_SECONDS = int(os.getenv("OCI_RETRY_INTERVAL_SECONDS", "120"))

# Singapore West home region / single AD. We also discover the
# AD from OCI and verify it, rather than trusting this constant.
EXPECTED_REGION = "ap-singapore-2"
EXPECTED_AD = "YQld:AP-SINGAPORE-2-AD-1"

# All A1 configurations below fit within the current Always Free
# A1 allowance (2 OCPUs / 12 GB total). The smaller configurations
# are tried first because a smaller request may be easier to place.
A1_CANDIDATES = [
    ("VM.Standard.A1.Flex", 1, 6),
    ("VM.Standard.A1.Flex", 1, 4),
    ("VM.Standard.A1.Flex", 1, 2),
    ("VM.Standard.A1.Flex", 2, 12),
]

# Final fallback. Oracle documents E2.1.Micro as Always Free.
E2_CANDIDATE = ("VM.Standard.E2.1.Micro", None, None)


@dataclass(frozen=True)
class Candidate:
    shape: str
    ocpus: Optional[int]
    memory_gb: Optional[int]
    label: str



def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        print(f"❌ Missing required GitHub secret/environment variable: {name}")
        sys.exit(1)
    return value.strip()



def build_config() -> dict:
    return {
        "user": required_env("OCI_USER_ID"),
        "key_content": required_env("OCI_PRIVATE_KEY"),
        "fingerprint": required_env("OCI_FINGERPRINT"),
        "tenancy": required_env("OCI_TENANCY_ID"),
        "region": required_env("OCI_REGION"),
    }



def print_service_error(prefix: str, exc: oci.exceptions.ServiceError) -> None:
    request_id = "unknown"
    try:
        request_id = exc.headers.get("opc-request-id", "unknown")
    except Exception:
        pass

    print(prefix)
    print(f"  HTTP status : {exc.status}")
    print(f"  OCI code    : {exc.code}")
    print(f"  Message     : {exc.message}")
    print(f"  Request ID  : {request_id}")



def is_capacity_error(exc: oci.exceptions.ServiceError) -> bool:
    code = (exc.code or "").lower()
    message = (exc.message or "").lower()
    return (
        "outofhostcapacity" in code
        or "out of host capacity" in message
        or "outofhostcapacity" in message
    )



def discover_availability_domain(identity_client: oci.identity.IdentityClient, tenancy_id: str) -> str:
    response = identity_client.list_availability_domains(compartment_id=tenancy_id)
    names = [item.name for item in response.data]

    print("Availability domains returned by OCI:")
    for name in names:
        print(f"  • {name}")

    if not names:
        raise RuntimeError("OCI returned no Availability Domains.")

    # Prefer the known Singapore West AD, but do not fail merely
    # because Oracle changes the display/name format in the future.
    if EXPECTED_AD in names:
        return EXPECTED_AD

    if len(names) == 1:
        return names[0]

    # In another region, selecting the first AD is better than
    # hard-coding an unavailable name; this repository is intended
    # primarily for the user's one-AD Singapore West tenancy.
    return names[0]



def validate_subnet(
    network_client: oci.core.VirtualNetworkClient,
    subnet_id: str,
) -> None:
    response = network_client.get_subnet(subnet_id)
    subnet = response.data

    print("\nSUBNET CHECK")
    print("-" * 60)
    print(f"Subnet name             : {subnet.display_name}")
    print(f"CIDR                    : {subnet.cidr_block}")
    print(f"Public IP prohibited    : {subnet.prohibit_public_ip_on_vnic}")

    if subnet.prohibit_public_ip_on_vnic:
        raise RuntimeError(
            "The configured subnet prohibits public IPv4 addresses. "
            "Choose a public subnet that allows a public IP."
        )

    print("✅ Subnet can assign a public IPv4 address.")



def find_ubuntu_image(
    compute_client: oci.core.ComputeClient,
    compartment_id: str,
    shape: str,
) -> Optional[object]:
    """
    Discover a current standard Ubuntu platform image.

    architecture:
        "aarch64" for Arm/A1
        "x86" for AMD/Intel/E2
    """

    response = compute_client.list_images(
        compartment_id=compartment_id,
        operating_system="Ubuntu",
        sort_by="TIMECREATED",
        sort_order="DESC",
        limit=50,
    )

    images = list(response.data)

    print(f"  OCI returned {len(images)} Ubuntu image entries.")

    candidates = []

    for image in images:
        name = (image.display_name or "").lower()

        # We only want usable platform images.
        lifecycle = (getattr(image, "lifecycle_state", "") or "").upper()

        if lifecycle and lifecycle != "AVAILABLE":
            continue

        # Avoid Minimal Ubuntu for A1 because OCI recommends
        # standard Ubuntu for Arm-based shapes.
        if "minimal" in name:
            continue

        if "ubuntu" not in name:
            continue

        if architecture == "aarch64":
            # Arm images are explicitly marked aarch64.
            if "aarch64" not in name:
                continue

        elif architecture == "x86":
            # OCI image names without aarch64 are the x86 builds.
            if "aarch64" in name:
                continue

        else:
            raise ValueError(
                f"Unsupported image architecture: {architecture}"
            )

        candidates.append(image)

    if not candidates:
        return None

    # Prefer 24.04, then 22.04, then the newest remaining Ubuntu image.
    def score(image):
        name = (image.display_name or "").lower()

        if "24.04" in name:
            version_score = 2
        elif "22.04" in name:
            version_score = 1
        else:
            version_score = 0

        return (
            version_score,
            getattr(image, "time_created", "") or "",
        )

    candidates.sort(key=score, reverse=True)

    return candidates[0]



def choose_candidates() -> List[Candidate]:
    candidates = [
        Candidate(shape, ocpus, memory, f"{shape} / {ocpus} OCPU / {memory} GB")
        for shape, ocpus, memory in A1_CANDIDATES
    ]
    candidates.append(
        Candidate(E2_CANDIDATE[0], None, None, E2_CANDIDATE[0])
    )
    return candidates



def existing_instance(
    compute_client: oci.core.ComputeClient,
    compartment_id: str,
    availability_domain: str,
) -> Optional[object]:
    response = compute_client.list_instances(
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        display_name=DISPLAY_NAME,
    )

    for instance in response.data:
        state = (instance.lifecycle_state or "").upper()
        if state not in {"TERMINATED", "TERMINATING"}:
            return instance

    return None



def build_launch_details(
    candidate: Candidate,
    compartment_id: str,
    availability_domain: str,
    image_id: str,
    subnet_id: str,
    public_ssh_key: str,
) -> oci.core.models.LaunchInstanceDetails:
    kwargs = dict(
        display_name=DISPLAY_NAME,
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        shape=candidate.shape,
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=image_id,
            boot_volume_size_in_gbs=50,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
            assign_private_dns_record=True,
            display_name=VNIC_NAME,
        ),
        metadata={"ssh_authorized_keys": public_ssh_key},
    )

    if candidate.shape == "VM.Standard.A1.Flex":
        kwargs["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=candidate.ocpus,
            memory_in_gbs=candidate.memory_gb,
        )

    return oci.core.models.LaunchInstanceDetails(**kwargs)



def main() -> int:
    print("=" * 72)
    print("OCI ALWAYS FREE INSTANCE PROVISIONER")
    print("=" * 72)

    config = build_config()
    tenancy_id = config["tenancy"]
    region = config["region"]
    subnet_id = required_env("OCI_SUBNET_ID")
    public_ssh_key = required_env("OCI_PUBLIC_SSH_KEY")

    print(f"Region                    : {region}")
    print(f"Instance name             : {DISPLAY_NAME}")
    print(f"Retry interval            : {RETRY_INTERVAL_SECONDS} seconds")
    print(f"Maximum run time          : {RUN_SECONDS // 60} minutes")

    if region != EXPECTED_REGION:
        print(
            f"⚠️ Warning: this repository is designed for {EXPECTED_REGION}, "
            f"but OCI_REGION is {region}."
        )

    if RETRY_INTERVAL_SECONDS < 60:
        print("❌ Retry interval must be at least 60 seconds for this script.")
        return 1

    try:
        identity_client = oci.identity.IdentityClient(config)
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)

        # A lightweight authenticated API call. No secret values are printed.
        identity_client.get_user(config["user"])
        print("✅ OCI authentication succeeded.")

    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ OCI authentication / identity check failed.", exc)
        return 1
    except Exception as exc:
        print(f"❌ OCI client initialization failed: {exc}")
        return 1

    # ------------------------------------------------------------
    # Discover and validate the single AD used by Singapore West.
    # ------------------------------------------------------------
    try:
        availability_domain = discover_availability_domain(identity_client, tenancy_id)
        print(f"\nSelected Availability Domain: {availability_domain}")

        if region == EXPECTED_REGION and availability_domain != EXPECTED_AD:
            print(
                "⚠️ Warning: the expected Singapore West AD was not returned. "
                "The script will use the only/first AD OCI returned."
            )
    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ Availability Domain discovery failed.", exc)
        return 1
    except Exception as exc:
        print(f"❌ Availability Domain discovery failed: {exc}")
        return 1

    # ------------------------------------------------------------
    # Validate subnet.
    # ------------------------------------------------------------
    try:
        validate_subnet(network_client, subnet_id)
    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ Subnet validation failed.", exc)
        return 1
    except Exception as exc:
        print(f"❌ Subnet validation failed: {exc}")
        return 1

    # ------------------------------------------------------------
    # Existing-instance check.
    # ------------------------------------------------------------
    try:
        instance = existing_instance(compute_client, tenancy_id, availability_domain)
        if instance:
            print("\n✅ Existing instance found.")
            print(f"   Name          : {instance.display_name}")
            print(f"   Lifecycle     : {instance.lifecycle_state}")
            print(f"   Instance OCID : {instance.id}")
            print("🛑 Provisioning stopped because the target instance already exists.")
            return 0

        print("\n✅ No existing target instance found.")

    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ Existing-instance check failed.", exc)
        return 1

    # ------------------------------------------------------------
# Discover compatible Ubuntu images.
#
# A1 = ARM/aarch64
# E2.1.Micro = x86
#
# Discover each architecture only once.
# ------------------------------------------------------------
candidates = choose_candidates()
images_by_shape = {}

print("\nIMAGE DISCOVERY")
print("-" * 60)

# ------------------------------------------------------------
# A1 / ARM image
# ------------------------------------------------------------
try:
    a1_image = find_ubuntu_image(
        compute_client,
        tenancy_id,
        "aarch64",
    )

    if a1_image:
        images_by_shape["VM.Standard.A1.Flex"] = a1_image

        print("✅ ARM/A1 Ubuntu image found:")
        print(f"   Name : {a1_image.display_name}")
        print(f"   OCID : {a1_image.id}")
    else:
        print("⚠️ No standard Ubuntu aarch64 image found for A1.")

except oci.exceptions.ServiceError as exc:
    print_service_error(
        "❌ A1 Ubuntu image discovery failed.",
        exc,
    )
    return 1

# ------------------------------------------------------------
# E2 Micro / x86 image
# ------------------------------------------------------------
try:
    e2_image = find_ubuntu_image(
        compute_client,
        tenancy_id,
        "x86",
    )

    if e2_image:
        images_by_shape["VM.Standard.E2.1.Micro"] = e2_image

        print("✅ x86/E2 Ubuntu image found:")
        print(f"   Name : {e2_image.display_name}")
        print(f"   OCID : {e2_image.id}")
    else:
        print("⚠️ No standard Ubuntu x86 image found for E2.")

except oci.exceptions.ServiceError as exc:
    print_service_error(
        "❌ E2 Ubuntu image discovery failed.",
        exc,
    )
    return 1

    # ------------------------------------------------------------
    # Main 120-second polling loop.
    # ------------------------------------------------------------
    print("\nSTARTING CAPACITY POLLING")
    print("-" * 60)
    print("Only genuine OutOfHostCapacity responses will be retried automatically.")
    print("Other errors stop the run so they can be fixed.")

    start_time = time.monotonic()
    attempt = 0
    candidate_index = 0

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed >= RUN_SECONDS:
            print("\n⏱️ Safe run-time window reached.")
            print("The workflow will exit cleanly; the next 6-hour scheduled run will continue.")
            return 0

        # Re-check for an already-created target before each launch request.
        try:
            existing = existing_instance(
                compute_client,
                tenancy_id,
                availability_domain,
            )
            if existing:
                print("\n✅ Target instance now exists.")
                print(f"   Instance OCID : {existing.id}")
                print(f"   Lifecycle     : {existing.lifecycle_state}")
                print("🛑 Stopping all further provisioning attempts.")
                return 0
        except oci.exceptions.ServiceError as exc:
            print_service_error("❌ Could not check for an existing instance.", exc)
            return 1

        candidate = candidates[candidate_index % len(candidates)]
        candidate_index += 1
        attempt += 1

        image = images_by_shape.get(candidate.shape)
        if not image:
            print(
                f"\n[Attempt {attempt}] Skipping {candidate.label}: "
                "no compatible image was discovered."
            )
            time.sleep(RETRY_INTERVAL_SECONDS)
            continue

        print(f"\n[Attempt {attempt}] {candidate.label}")
        print(f"  AD    : {availability_domain}")
        print(f"  Image : {image.display_name}")

        request = build_launch_details(
            candidate=candidate,
            compartment_id=tenancy_id,
            availability_domain=availability_domain,
            image_id=image.id,
            subnet_id=subnet_id,
            public_ssh_key=public_ssh_key,
        )

        # Unique token for this individual launch request. If OCI accepts
        # the request, we immediately stop; if OCI says host capacity is out,
        # no instance should have been created and we move on.
        retry_token = f"gh-{uuid.uuid4()}"

        attempt_started = time.monotonic()

        try:
            response = compute_client.launch_instance(
                launch_instance_details=request,
                opc_retry_token=retry_token,
            )

            instance = response.data
            print("\n🎉 SUCCESS — OCI accepted the instance launch!")
            print(f"   Instance OCID : {instance.id}")
            print(f"   Name          : {instance.display_name}")
            print(f"   Lifecycle     : {instance.lifecycle_state}")
            print(f"   Shape         : {instance.shape}")
            print(f"   AD            : {instance.availability_domain}")
            print("🛑 Provisioning loop stopped immediately.")
            return 0

        except oci.exceptions.ServiceError as exc:
            if is_capacity_error(exc):
                print_service_error("⚠️ Out of host capacity.", exc)
            else:
                print_service_error("❌ Launch request failed.", exc)
                print("This error is not being blindly retried.")
                return 1

        elapsed_attempt = time.monotonic() - attempt_started
        remaining = RETRY_INTERVAL_SECONDS - elapsed_attempt

        if remaining > 0:
            print(
                f"Waiting {remaining:.1f} seconds before the next launch request..."
            )
            time.sleep(remaining)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(130)
