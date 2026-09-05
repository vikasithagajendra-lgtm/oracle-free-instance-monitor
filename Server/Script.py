"""OCI Always Free A1 instance provisioner.

Purpose:
    Keep checking for a single VM.Standard.A1.Flex 1-OCPU / 6-GB instance
    in the tenancy's home region (Singapore West / ap-singapore-2).

Behavior:
    * Runs for at most 180 minutes (3 hours) per GitHub Actions job.
    * Makes one launch request roughly every 120 seconds after a genuine
      OutOfHostCapacity response.
    * Stops immediately if the target instance already exists or is created.
    * Writes a GitHub Actions step output so the workflow can self-trigger a
      single next run after the 3-hour polling window. The workflow controls
      the handoff; the Python code does not call the GitHub API directly.
    * Uses GitHub Secrets for all OCI credentials.
    * Never prints credential values.

This script does not configure V2Ray. It only provisions the OCI VM.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

import oci


# ---------------------------------------------------------------------------
# User's fixed OCI environment
# ---------------------------------------------------------------------------

EXPECTED_REGION = "ap-singapore-2"
EXPECTED_AD = "YQld:AP-SINGAPORE-2-AD-1"
INSTANCE_NAME = "FX-Backend-Server"
VNIC_NAME = "fx-backend-vnic"

# Only the requested configuration is attempted.
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 1
MEMORY_GB = 6
BOOT_VOLUME_GB = 50

# 2 minutes between capacity retries.
RETRY_INTERVAL_SECONDS = 120

# Poll for exactly 3 hours; the workflow has additional time for handoff.
MAX_RUN_SECONDS = 180 * 60  # 3 hours


# ---------------------------------------------------------------------------
# Environment / logging helpers
# ---------------------------------------------------------------------------


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub secret/environment variable: {name}")
    return value


def set_workflow_status(status: str) -> None:
    """Write a status value to the current GitHub Actions step output.

    The workflow uses this to decide whether a fresh run should be
    dispatched after the current polling window. No secret is written.
    """

    output_file = os.getenv("GITHUB_OUTPUT")
    if not output_file:
        return

    with open(output_file, "a", encoding="utf-8") as handle:
        handle.write(f"status={status}\n")


def build_config() -> dict[str, str]:
    return {
        "user": required_env("OCI_USER_ID"),
        "key_content": required_env("OCI_PRIVATE_KEY"),
        "fingerprint": required_env("OCI_FINGERPRINT"),
        "tenancy": required_env("OCI_TENANCY_ID"),
        "region": required_env("OCI_REGION"),
    }


def print_service_error(prefix: str, exc: oci.exceptions.ServiceError) -> None:
    request_id = "unknown"
    if exc.headers:
        request_id = exc.headers.get("opc-request-id", "unknown")

    print(prefix)
    print(f"  HTTP status : {exc.status}")
    print(f"  OCI code    : {exc.code or 'unknown'}")
    print(f"  Message     : {exc.message or str(exc)}")
    print(f"  Request ID  : {request_id}")


def is_capacity_error(exc: oci.exceptions.ServiceError) -> bool:
    code = (exc.code or "").lower()
    message = (exc.message or str(exc)).lower()

    return (
        "outofhostcapacity" in code
        or "out of host capacity" in message
        or ("out of capacity" in message and "host" in message)
    )


# ---------------------------------------------------------------------------
# OCI discovery / validation
# ---------------------------------------------------------------------------


def discover_availability_domain(
    identity_client: oci.identity.IdentityClient,
    tenancy_id: str,
) -> str:
    response = identity_client.list_availability_domains(
        compartment_id=tenancy_id
    )

    ads = [item.name for item in response.data]

    print("AVAILABILITY DOMAIN CHECK")
    print("-" * 60)

    for ad in ads:
        print(f"  • {ad}")

    if EXPECTED_AD not in ads:
        raise RuntimeError(
            f"Expected Singapore West AD '{EXPECTED_AD}' was not returned. "
            f"OCI returned: {ads}"
        )

    print(f"  Selected AD : {EXPECTED_AD}")
    print()
    return EXPECTED_AD


def validate_subnet(
    vcn_client: oci.core.VirtualNetworkClient,
    subnet_id: str,
) -> None:
    subnet = vcn_client.get_subnet(subnet_id).data

    print("SUBNET CHECK")
    print("-" * 60)
    print(f"  Name                    : {subnet.display_name}")
    print(f"  CIDR                    : {subnet.cidr_block}")
    print(f"  Public IP prohibited    : {subnet.prohibit_public_ip_on_vnic}")

    if subnet.prohibit_public_ip_on_vnic:
        raise RuntimeError(
            "The selected subnet prohibits public IP addresses. "
            "Use a public subnet that allows public IP assignment."
        )

    print("  ✅ Subnet can assign a public IPv4 address.")
    print()


def get_existing_instances(
    compute_client: oci.core.ComputeClient,
    tenancy_id: str,
) -> list[Any]:
    response = compute_client.list_instances(
        compartment_id=tenancy_id,
        display_name=INSTANCE_NAME,
    )
    return list(response.data)


def find_existing_target(
    compute_client: oci.core.ComputeClient,
    tenancy_id: str,
) -> Any | None:
    instances = get_existing_instances(compute_client, tenancy_id)

    if not instances:
        return None

    print("EXISTING INSTANCE CHECK")
    print("-" * 60)

    for instance in instances:
        print(
            f"  Found: {instance.display_name} | "
            f"{instance.lifecycle_state} | {instance.id}"
        )

    active_or_reserved_states = {
        "PROVISIONING",
        "RUNNING",
        "STARTING",
        "STOPPING",
        "STOPPED",
        "UPDATING",
        "RESTARTING",
        "MOVING",
        "TERMINATING",
    }

    for instance in instances:
        if str(instance.lifecycle_state) in active_or_reserved_states:
            return instance

    return None


def find_a1_ubuntu_image(
    compute_client: oci.core.ComputeClient,
    tenancy_id: str,
) -> Any | None:
    """Find the newest standard Ubuntu ARM64 image.

    OCI's image-list endpoint currently publishes the standard Ubuntu ARM
    platform images in the region, but filtering the request server-side with
    operating_system="Ubuntu" can return an empty result in some tenancies.

    To avoid that false negative, fetch the platform image list without the
    OS filter and then filter locally. We intentionally avoid hard-coding an
    image OCID because Oracle regularly replaces platform image builds.
    """

    print("  Querying OCI for available platform images...")

    try:
        response = oci.pagination.list_call_get_all_results(
            compute_client.list_images,
            compartment_id=tenancy_id,
            sort_by="TIMECREATED",
            sort_order="DESC",
            limit=100,
        )
        images = list(response.data)
    except TypeError:
        # Compatibility fallback for SDK variants where the pagination helper
        # rejects an optional argument.
        response = compute_client.list_images(
            compartment_id=tenancy_id,
            sort_by="TIMECREATED",
            sort_order="DESC",
            limit=100,
        )
        images = list(response.data)

    print(f"  OCI returned {len(images)} total image entries.")

    candidates: list[Any] = []

    for image in images:
        display_name = str(getattr(image, "display_name", "") or "")
        name = display_name.lower()
        lifecycle = str(getattr(image, "lifecycle_state", "") or "").upper()
        operating_system = str(
            getattr(image, "operating_system", "") or ""
        ).lower()

        if lifecycle and lifecycle != "AVAILABLE":
            continue

        if operating_system != "ubuntu" and "ubuntu" not in name:
            continue

        # OCI documents that the standard Ubuntu image (not Minimal Ubuntu)
        # supports Arm-based shapes. A1 needs the ARM64/aarch64 build.
        if "minimal" in name:
            continue

        if "aarch64" not in name and "arm64" not in name:
            continue

        candidates.append(image)

    if not candidates:
        print("  No Ubuntu ARM64 images were found in the returned image list.")
        print("  This means image discovery is empty, not host capacity.")
        return None

    def score(image: Any) -> tuple[int, int, str]:
        name = str(getattr(image, "display_name", "") or "").lower()

        # Prefer current LTS first, then previous LTS.
        if "24.04" in name:
            version_score = 2
        elif "22.04" in name:
            version_score = 1
        else:
            version_score = 0

        # Prefer standard Ubuntu ARM64 naming over any unexpected variant.
        arm_score = 2 if "aarch64" in name else 1

        created = str(getattr(image, "time_created", "") or "")
        return version_score, arm_score, created

    candidates.sort(key=score, reverse=True)
    selected = candidates[0]

    return selected


# ---------------------------------------------------------------------------
# Launch construction
# ---------------------------------------------------------------------------


def build_launch_details(
    compartment_id: str,
    availability_domain: str,
    subnet_id: str,
    image_id: str,
    public_ssh_key: str,
) -> oci.core.models.LaunchInstanceDetails:
    source_details = oci.core.models.InstanceSourceViaImageDetails(
        source_type="image",
        image_id=image_id,
        boot_volume_size_in_gbs=BOOT_VOLUME_GB,
    )

    shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=OCPUS,
        memory_in_gbs=MEMORY_GB,
    )

    return oci.core.models.LaunchInstanceDetails(
        display_name=INSTANCE_NAME,
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        shape=SHAPE,
        shape_config=shape_config,
        source_details=source_details,
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
            assign_private_dns_record=True,
            display_name=VNIC_NAME,
        ),
        metadata={
            "ssh_authorized_keys": public_ssh_key,
        },
    )


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("OCI ALWAYS FREE A1 INSTANCE PROVISIONER")
    print("=" * 78)
    print(f"Region              : {EXPECTED_REGION}")
    print(f"Availability Domain  : {EXPECTED_AD}")
    print(f"Instance name        : {INSTANCE_NAME}")
    print(f"Shape                : {SHAPE}")
    print(f"OCPUs                : {OCPUS}")
    print(f"Memory               : {MEMORY_GB} GB")
    print(f"Network target       : up to {OCPUS} Gbps (shape allocation)")
    print(f"Retry interval       : {RETRY_INTERVAL_SECONDS} seconds")
    print(f"Maximum run time     : {MAX_RUN_SECONDS // 60} minutes")
    print()

    try:
        config = build_config()
        subnet_id = required_env("OCI_SUBNET_ID")
        public_ssh_key = required_env("OCI_PUBLIC_SSH_KEY")
    except RuntimeError as exc:
        print(f"❌ CONFIGURATION ERROR: {exc}")
        return 1

    if config["region"] != EXPECTED_REGION:
        print("❌ REGION MISMATCH")
        print(f"  OCI_REGION is {config['region']}")
        print(f"  Expected   is {EXPECTED_REGION}")
        return 1

    # Disable SDK-level automatic retries so that our own 120-second loop is
    # the only retry mechanism controlling launch requests.
    retry_strategy = oci.retry.NoneRetryStrategy()

    try:
        identity_client = oci.identity.IdentityClient(
            config,
            retry_strategy=retry_strategy,
        )
        compute_client = oci.core.ComputeClient(
            config,
            retry_strategy=retry_strategy,
        )
        vcn_client = oci.core.VirtualNetworkClient(
            config,
            retry_strategy=retry_strategy,
        )

        print("AUTHENTICATION CHECK")
        identity_client.get_tenancy(config["tenancy"])
        print("  ✅ OCI authentication succeeded.")
        print()

    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ OCI authentication failed:", exc)
        return 1
    except Exception as exc:
        print(f"❌ OCI client initialization failed: {exc!r}")
        return 1

    tenancy_id = config["tenancy"]

    # Static validation is done once at the beginning.
    try:
        availability_domain = discover_availability_domain(
            identity_client,
            tenancy_id,
        )
        validate_subnet(vcn_client, subnet_id)
    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ OCI validation failed:", exc)
        return 1
    except RuntimeError as exc:
        print(f"❌ VALIDATION ERROR: {exc}")
        return 1

    # Check for an existing VM before doing any image/launch work.
    try:
        print("INITIAL INSTANCE CHECK")
        existing = find_existing_target(compute_client, tenancy_id)
        if existing is not None:
            print("✅ Target instance already exists.")
            print(f"   Instance ID : {existing.id}")
            print(f"   State       : {existing.lifecycle_state}")
            print("🛑 Provisioning stopped.")
            set_workflow_status("DONE")
            return 0
        print("  ✅ No existing target instance found.")
        print()
    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ Could not inspect existing instances:", exc)
        return 1

    # Discover the A1-compatible image once. Oracle currently publishes standard
    # Ubuntu aarch64 images, including Ubuntu 24.04. We do not hard-code an OCID.
    print("IMAGE DISCOVERY")
    print("-" * 60)

    try:
        image = find_a1_ubuntu_image(compute_client, tenancy_id)
    except oci.exceptions.ServiceError as exc:
        print_service_error("❌ Ubuntu image discovery failed:", exc)
        return 1

    if image is None:
        print("❌ No standard Ubuntu aarch64 image was returned by OCI.")
        print("The script will NOT spam the launch API when the image cannot be identified.")
        return 1

    print("  ✅ Selected Ubuntu ARM image:")
    print(f"     Name : {image.display_name}")
    print(f"     OCID : {image.id}")
    print()

    print("STARTING CAPACITY POLLING")
    print("-" * 60)
    print("Only genuine OutOfHostCapacity errors are retried every 120 seconds.")
    print("Other errors stop the run so they can be fixed.")
    print()

    start_time = time.monotonic()
    attempt = 0

    while time.monotonic() - start_time < MAX_RUN_SECONDS:
        attempt += 1

        elapsed_minutes = int((time.monotonic() - start_time) // 60)
        remaining_minutes = max(
            0,
            int((MAX_RUN_SECONDS - (time.monotonic() - start_time)) // 60),
        )

        print(
            f"[Attempt {attempt}] "
            f"{SHAPE} / {OCPUS} OCPU / {MEMORY_GB} GB "
            f"(elapsed {elapsed_minutes} min, ~{remaining_minutes} min left)"
        )

        # Re-check before every launch attempt. This protects against an
        # ambiguous previous API response that actually created the VM.
        try:
            existing = find_existing_target(compute_client, tenancy_id)
            if existing is not None:
                print()
                print("✅ Target instance detected.")
                print(f"   Instance ID : {existing.id}")
                print(f"   State       : {existing.lifecycle_state}")
                print("🛑 No further launch attempts will be made.")
                set_workflow_status("DONE")
                return 0
        except oci.exceptions.ServiceError as exc:
            print_service_error("❌ Existing-instance safety check failed:", exc)
            print("Stopping this run rather than risking duplicate infrastructure.")
            return 1

        try:
            details = build_launch_details(
                compartment_id=tenancy_id,
                availability_domain=availability_domain,
                subnet_id=subnet_id,
                image_id=image.id,
                public_ssh_key=public_ssh_key,
            )

            # Unique token per actual launch attempt. We also re-check for an
            # existing instance before every retry, preventing duplicate launches
            # after an ambiguous network failure.
            request_token = str(uuid.uuid4())

            response = compute_client.launch_instance(
                details,
                opc_retry_token=request_token,
            )

            created = response.data

            print()
            print("=" * 78)
            print("🎉 INSTANCE CREATION REQUEST ACCEPTED")
            print("=" * 78)
            print(f"Instance ID       : {created.id}")
            print(f"Display name      : {created.display_name}")
            print(f"Lifecycle state   : {created.lifecycle_state}")
            print(f"Shape             : {SHAPE}")
            print(f"OCPUs             : {OCPUS}")
            print(f"Memory            : {MEMORY_GB} GB")
            print()
            print("Oracle accepted the launch request.")
            print("🛑 Provisioning loop stopped immediately.")
            print("=" * 78)
            set_workflow_status("DONE")
            return 0

        except oci.exceptions.ServiceError as exc:
            print_service_error("  OCI launch response:", exc)

            if is_capacity_error(exc):
                print("  ⚠️ Out of host capacity.")
                print(f"  → Waiting {RETRY_INTERVAL_SECONDS} seconds before the next attempt.")
                print()

                remaining = MAX_RUN_SECONDS - (time.monotonic() - start_time)
                if remaining <= RETRY_INTERVAL_SECONDS:
                    print("Run window is almost finished; stopping cleanly.")
                    break

                time.sleep(RETRY_INTERVAL_SECONDS)
                continue

            if exc.status == 429:
                # A real API throttle response overrides the normal 120-second
                # polling interval. Respect Retry-After when OCI supplies it;
                # otherwise wait 10 minutes to avoid making throttling worse.
                retry_after_raw = None
                if exc.headers:
                    retry_after_raw = exc.headers.get("retry-after")

                try:
                    retry_after = max(300, int(retry_after_raw)) if retry_after_raw else 600
                except (TypeError, ValueError):
                    retry_after = 600

                print("  ⚠️ OCI rate limiting (HTTP 429).")
                print(f"  → Waiting {retry_after} seconds before the next run attempt.")
                print()

                remaining = MAX_RUN_SECONDS - (time.monotonic() - start_time)
                if remaining <= retry_after:
                    print("Not enough run time remains for a safe backoff; stopping cleanly.")
                    return 0

                time.sleep(retry_after)
                continue

            if exc.status in (401, 403):
                print("  ❌ Authentication/authorization error.")
                print("  Do NOT keep retrying. Fix the IAM policy/API key first.")
                return 1

            if exc.status == 400:
                print("  ❌ Invalid launch request (HTTP 400).")
                print("  Fix the configuration instead of retrying blindly.")
                return 1

            if exc.status == 404:
                print("  ❌ A referenced OCI resource was not found.")
                return 1

            if exc.status >= 500:
                print("  ⚠️ OCI returned another server-side error.")
                print("  Stopping this run to avoid duplicate/unknown requests.")
                print("  This is treated as a clean end of this polling window.")
                set_workflow_status("CONTINUE")
                return 0

            print("  ❌ Unclassified OCI error. Stopping this run.")
            return 1

        except Exception as exc:
            print(f"  ❌ Unexpected Python error: {exc!r}")
            print("  Stopping this run rather than risking duplicate infrastructure.")
            return 1

    print()
    print("=" * 78)
    print("RUN WINDOW FINISHED")
    print("=" * 78)
    print("No instance was created during this workflow run.")
    print("This polling window finished normally.")
    print("The workflow will request exactly one fresh run.")
    print("=" * 78)
    set_workflow_status("CONTINUE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
