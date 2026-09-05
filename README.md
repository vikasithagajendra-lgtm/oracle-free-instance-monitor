# OCI Always Free A1 Instance Monitor

This repository contains a GitHub Actions provisioner for one Oracle Cloud Infrastructure Always Free Ampere A1 VM.

## Target configuration

- Region: `ap-singapore-2` (Singapore West)
- Availability Domain: `YQld:AP-SINGAPORE-2-AD-1`
- Shape: `VM.Standard.A1.Flex`
- OCPU: 1
- Memory: 6 GB
- Boot volume: 50 GB
- Public IPv4: enabled through the selected subnet
- Image: newest standard Ubuntu ARM64/aarch64 image discovered from OCI

## Polling model

The workflow is deliberately **not scheduled every 6 hours**. A single workflow polls OCI for approximately 3 hours, with a 120-second delay after genuine `OutOfHostCapacity` responses. When the 3-hour window finishes normally, the workflow waits 10 seconds and dispatches exactly one fresh copy of itself. The same concurrency group prevents the new copy from running concurrently with the old copy.

A short 10-second buffer at the beginning of every run provides an additional handoff delay. GitHub controls runner allocation, so the actual wall-clock gap cannot be guaranteed to be exactly 10 seconds.

The chain stops when:

1. `FX-Backend-Server` already exists, or
2. OCI accepts the new instance launch request, or
3. a non-retryable configuration/IAM/resource error occurs.

## Security

Keep all OCI credentials in GitHub Actions Secrets. No OCI private key is stored in this repository. The workflow only requests `actions: write` so it can dispatch the next workflow run; it does not grant the OCI credentials any GitHub write access.

Recommended repository protections:

- Protect the `main` branch.
- Do not add `pull_request` or `pull_request_target` triggers to this workflow.
- Never merge untrusted changes to the credential-bearing workflow or provisioner.
- Never commit `.pem`, SSH private keys, `.env` files, or OCI credentials.

## Required GitHub Secrets

- `OCI_USER_ID`
- `OCI_PRIVATE_KEY`
- `OCI_FINGERPRINT`
- `OCI_TENANCY_ID`
- `OCI_REGION` = `ap-singapore-2`
- `OCI_SUBNET_ID`
- `OCI_PUBLIC_SSH_KEY`

## Important

The script provisions the OCI VM only. It does not install or configure V2Ray.
