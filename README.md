# OCI Always Free Instance Monitor

This repository uses GitHub Actions to periodically try to create one OCI Always Free Compute instance. It is designed for the user's Singapore West home region (`ap-singapore-2`) and its single Availability Domain.

## What it does

- Uses a dedicated OCI IAM user rather than the everyday OCI administrator user.
- Reads the OCI API private key only from a GitHub Actions Secret.
- Never writes OCI secrets into the repository.
- Validates OCI authentication.
- Validates that the configured subnet permits a public IP.
- Discovers the Availability Domain from OCI and expects `YQld:AP-SINGAPORE-2-AD-1` in Singapore West.
- Checks for an existing `FX-Backend-Server` before every launch attempt.
- Discovers current Ubuntu platform images compatible with each candidate shape.
- Tries small A1 configurations first, then the current Always Free A1 maximum, then the Always Free E2.1.Micro fallback.
- Retries only genuine host-capacity errors.
- Waits 120 seconds between launch requests.
- Runs for about 5h45m and then exits cleanly so the next 6-hour scheduled run can continue.
- Stops immediately after OCI accepts a launch request or after it detects the target instance already exists.
- Does not self-trigger another GitHub workflow.

## Important GitHub facts

GitHub's scheduled workflow trigger cannot run every two minutes; its documented minimum is every five minutes. This project therefore uses a single long-lived job that performs the two-minute polling internally. GitHub's current documented maximum for a GitHub-hosted job is six hours, so the workflow and Python program both stop before that limit.

Standard GitHub-hosted runners are free and unlimited for public repositories, subject to GitHub's service policies.

## OCI assumptions

Home region:

```text
ap-singapore-2
```

Expected Availability Domain:

```text
YQld:AP-SINGAPORE-2-AD-1
```

Primary target:

```text
VM.Standard.A1.Flex
```

Initial A1 candidates:

```text
1 OCPU / 6 GB
1 OCPU / 4 GB
1 OCPU / 2 GB
2 OCPU / 12 GB
```

Fallback:

```text
VM.Standard.E2.1.Micro
```

The A1 candidates stay inside the current Always Free A1 total of 2 OCPUs and 12 GB. The E2 Micro is also an Always Free shape.

## GitHub Secrets

Create these under:

**Repository → Settings → Secrets and variables → Actions**

```text
OCI_USER_ID
OCI_PRIVATE_KEY
OCI_FINGERPRINT
OCI_TENANCY_ID
OCI_REGION
OCI_SUBNET_ID
OCI_PUBLIC_SSH_KEY
```

Set:

```text
OCI_REGION = ap-singapore-2
```

`OCI_IMAGE_ID` is intentionally not used. The script discovers a current compatible Ubuntu image instead.

## Security rules

Never commit any of these:

- OCI private key PEM files
- SSH private keys
- passwords
- `.env` files
- copied GitHub secret values

The repository is intended to be public, so the source code and workflow are visible. The secret values remain in GitHub Secrets.

Protect the default branch and do not merge untrusted changes into the workflow that has access to OCI secrets.

## IAM recommendation

Use a dedicated OCI user such as:

```text
github-oci-provisioner
```

Put the user in a group such as:

```text
GitHubOCIProvisioners
```

The current beginner-friendly policy used for this repository is:

```text
Allow group GitHubOCIProvisioners to manage instance-family in tenancy
Allow group GitHubOCIProvisioners to use volume-family in tenancy
Allow group GitHubOCIProvisioners to use virtual-network-family in tenancy
Allow group GitHubOCIProvisioners to read app-catalog-listing in tenancy
```

After everything works, reduce the policy to specific compartments if possible.

## First test

1. Create the public repository.
2. Upload the files exactly as shown in the repository tree.
3. Add the seven GitHub Secrets.
4. Go to **Actions → OCI Always Free Instance Monitor**.
5. Select **Run workflow**.
6. Open the job log and inspect the validation sections.
7. Do not paste private keys or secret values into issues, chat, or the repository.

A healthy first run should show:

```text
✅ OCI authentication succeeded.
Availability domains returned by OCI:
  • YQld:AP-SINGAPORE-2-AD-1
✅ Subnet can assign a public IPv4 address.
✅ No existing target instance found.
IMAGE DISCOVERY
...
STARTING CAPACITY POLLING
...
```

## Interpreting failures

- `401` or `403`: credentials/IAM problem. Fix IAM; do not keep retrying.
- `400`: invalid request/configuration. Fix the request.
- `404`: a referenced resource was not found. Fix the resource ID/configuration.
- `429`: rate limiting. Fix/back off rather than increasing request frequency.
- `OutOfHostCapacity`: this is the expected retryable capacity problem. The next candidate is tried and the script continues after the 120-second interval.
- Other `5xx`: the script stops the current run instead of assuming every server error means capacity.

## What happens after success

When OCI accepts a launch request, the script exits immediately. Later scheduled runs check for `FX-Backend-Server` and exit without creating another instance.

## This repository does not install V2Ray

The provisioner only creates the OCI VM. After the VM exists and SSH access has been verified, configure the server software separately.
