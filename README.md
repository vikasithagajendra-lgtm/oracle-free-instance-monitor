# OCI Always Free A1 Instance Monitor

This repository is a GitHub Actions automation project for creating one Oracle Cloud Infrastructure Always Free Ampere A1 VM.

## Target configuration

- Region: `ap-singapore-2` (Singapore West)
- Availability Domain: `YQld:AP-SINGAPORE-2-AD-1`
- Shape: `VM.Standard.A1.Flex`
- OCPU: 1
- Memory: 6 GB
- Boot volume: 50 GB
- Public IPv4: enabled through the selected public subnet
- Image: newest standard Ubuntu ARM64/aarch64 image discovered from OCI

## Behavior

The workflow starts approximately every 6 hours. Each run polls for host capacity for up to 345 minutes and waits about 120 seconds after a genuine `OutOfHostCapacity` response.

The script stops immediately when:

1. the target instance already exists, or
2. OCI accepts a new instance launch request.

It does not recursively start another GitHub workflow.

## Required GitHub Secrets

Create these repository Actions secrets:

- `OCI_USER_ID`
- `OCI_PRIVATE_KEY`
- `OCI_FINGERPRINT`
- `OCI_TENANCY_ID`
- `OCI_REGION` = `ap-singapore-2`
- `OCI_SUBNET_ID`
- `OCI_PUBLIC_SSH_KEY`

Never commit an OCI private key or SSH private key to the repository.


## Image discovery fix

The provisioner does not rely on OCI's server-side `operating_system=Ubuntu`
image filter. It fetches the available platform images and selects the newest
standard Ubuntu ARM64/aarch64 image locally. This avoids the false
`0 Ubuntu image entries` result seen in some tenancies while still avoiding a
hard-coded image OCID.
