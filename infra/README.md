# Zuno DevOps Assessment: AWS Deployment with Pulumi

## Overview

This project deploys the provided Go web application (from `zuno-tech/devops-assessment`) to AWS using **Pulumi (Python)**. The container image is stored in a private **ECR** repository and runs on **ECS Fargate**. The application is reachable from the public internet on **port 8080**.

The application source (`main.go`, `go.mod`) and the `Dockerfile` are **unchanged**, as required by the assessment.

## Architecture

| Resource | Purpose |
|---|---|
| ECR repository | Private image registry. Scan on push is enabled. |
| ECS cluster | Logical home for the service. |
| ECS Fargate task and service | Runs one copy of the container (0.25 vCPU, 512 MB). |
| IAM task execution role | Lets ECS pull the image from ECR and write logs. Uses the AWS-managed `AmazonECSTaskExecutionRolePolicy`. No other permissions. |
| Security group | Allows inbound TCP **8080** only. All outbound allowed (needed to pull the image). |
| CloudWatch log group | Container logs, retained for 1 day. |
| Default VPC and subnets | Looked up, not created, to avoid NAT gateway costs. |

The task gets a public IP directly, so no load balancer is used.

## Prerequisites

- An AWS account and an IAM user with programmatic access (configured with `aws configure`)
- AWS CLI v2
- Pulumi CLI, logged in (`pulumi login`)
- Python 3.9+
- Docker (running)

## Build and Deploy

Deployment is done in two stages. The ECR repository has to exist before an image can be pushed, and the ECS service is only created once the `imageUri` config value is set.

**1. Set up the project**

```bash
cd infra
python -m venv venv
source venv/Scripts/activate      # Git Bash on Windows. On Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
pulumi stack select dev           # or: pulumi stack init dev
pulumi config set aws:region eu-west-2     # e.g. eu-west-2
```

**2. Create the ECR repository (stage one)**

```bash
pulumi up
```

At this stage `imageUri` is not set, so only the shared resources are created and the repository URL is exported.

**3. Build and push the image**

```bash
REPO=$(pulumi stack output repositoryUrl)
REGISTRY=${REPO%%/*}

aws ecr get-login-password --region eu-west-2 | docker login --username AWS --password-stdin $REGISTRY

cd ..                              # repo root, where the Dockerfile is
docker build --platform linux/amd64 -t webapp .
docker tag webapp $REPO:assessment
docker push $REPO:assessment
cd infra
```

`--platform linux/amd64` ensures the image matches the Fargate CPU architecture (the Dockerfile downloads Go using the build architecture).

**4. Deploy the service (stage two)**

```bash
pulumi config set imageUri $REPO:assessment
pulumi up
```

Wait a minute or two for the task to start.

## Test

Fargate assigns the public IP to the task, so look it up:

```bash
CLUSTER=$(pulumi stack output clusterName)
SERVICE=$(pulumi stack output serviceName)

TASK_ARN=$(aws ecs list-tasks --cluster $CLUSTER --service-name $SERVICE --query 'taskArns[0]' --output text)
ENI=$(aws ecs describe-tasks --cluster $CLUSTER --tasks $TASK_ARN --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" --output text)
PUBLIC_IP=$(aws ec2 describe-network-interfaces --network-interface-ids $ENI --query 'NetworkInterfaces[0].Association.PublicIp' --output text)
echo $PUBLIC_IP
```

Then call the endpoints:

```bash
curl -i http://$PUBLIC_IP:8080/
curl -i http://$PUBLIC_IP:8080/external-check
```

Expected results:
- `/external-check` returns `200 OK` with `External check successful`.
- `/` returns `200 OK` with an `Uptime: ...` value. **About 1 in 10 requests intentionally returns `500 We're down!`**, because `main.go` simulates failures with a random roll. This is the application's behaviour, not a deployment fault. To see it:

```bash
for i in {1..20}; do curl -s -o /dev/null -w "%{http_code}\n" http://$PUBLIC_IP:8080/; done
```

Security group check: any other port should not respond, for example `curl -i --max-time 5 http://$PUBLIC_IP:8081` fails or times out.

## Teardown

```bash
cd infra
pulumi destroy
```

The ECR repository uses `force_delete`, so it is removed even though it contains an image. After destroying, check the AWS console (ECS, ECR, CloudWatch, EC2 security groups, IAM roles) in the deployment region to confirm nothing is left.

## Design Decisions

- **Pulumi with Python:** preferred by the assessment, and the same language is used for config and logic.
- **ECS Fargate:** no servers to patch, no SSH access, and no keys to manage. It suits a single stateless container.
- **No load balancer, no NAT gateway:** both bill hourly even when idle. The task uses the default VPC and a public IP instead, which keeps the cost low for a short-lived assessment.
- **Two-stage deploy:** the service is only created when `imageUri` is set. This avoids a service stuck failing to pull an image that does not exist yet.
- **Least privilege on the task:** only an execution role is created, using the AWS-managed policy. The container has no task role, so the application itself has no AWS permissions.
- **Security group:** only the application port (8080) is open inbound.
- **Image scanning:** ECR scan on push is enabled.
- **Deployment circuit breaker:** a bad deployment rolls back automatically.
- **No secrets in code:** credentials come from the local AWS CLI profile. The Pulumi config holds only the region and image URI, and neither is secret.

## Security Limitations

- Traffic is **HTTP only**. HTTPS would need a load balancer or CloudFront with a certificate and a domain.
- Port 8080 is open to `0.0.0.0/0` because the application must be public.
- The task has a public IP that changes whenever the task is replaced.
- The container runs as **root**, because of the provided Dockerfile (a full `ubuntu:latest` image that downloads Go during the build). I left the Dockerfile unchanged.
- Image tags are mutable, and scan findings are not enforced in a pipeline.
- The IAM user used to deploy had broad permissions (AdministratorAccess) for convenience. This is acceptable for a throwaway assessment, not for production.

## Cost

ECS Fargate and public IPv4 addresses are **not** covered by the AWS free tier, but usage here is very small (0.25 vCPU, 512 MB, one task, 1-day log retention). Infrastructure was destroyed straight after testing, so the cost is a few pence at most. No load balancer or NAT gateway was created.

## What I Would Improve With More Time

- Put the service behind an Application Load Balancer with HTTPS (ACM certificate and a domain), with a health check on `/external-check` (not `/`, which fails by design).
- Multi-stage Dockerfile with a minimal non-root runtime image.
- Build and push the image from Pulumi itself (for example with `pulumi-docker-build`) to make this a single `pulumi up`.
- CI/CD with GitHub Actions, using OIDC instead of long-lived access keys, plus image scanning gates.
- Tighter IAM for the deploying identity, and a remote state backend with encryption.
- Autoscaling, CloudWatch alarms, and a private subnet design with VPC endpoints.
- Immutable image tags (git SHA) instead of `assessment`.

## Issues Encountered

- **First time using Pulumi.** I had not used Pulumi before, so I spent time learning how projects, stacks, config values and `pulumi up` / `pulumi destroy` fit together, and finding my way around the generated project files.
- **Pulumi login.** Commands failed until I created a Pulumi Cloud account and ran `pulumi login`. After that I confirmed it with `pulumi whoami`.
- **Commands had to be re-run in every new terminal.** Shell variables such as `REPO`, `CLUSTER`, `SERVICE` and `PUBLIC_IP` are lost when the terminal is closed or reopened, so I had to re-run the `pulumi stack output` and AWS CLI lookups each time before testing. The task's public IP is also not exported by Pulumi, so it has to be looked up with the AWS CLI. With more time I would export it, or put these steps in a small helper script.
- **Two-stage deployment.** The ECR repository must exist before the image can be pushed, so the first `pulumi up` creates the repository and the second creates the service once `imageUri` is set. This took some trial and error to get right.
- **AWS credentials error.** `aws sts get-caller-identity` returned `SignatureDoesNotMatch` because the secret access key had been copied incorrectly. I fixed it by creating a new access key, saving it with `aws configure set`, and deleting the old one.
- **Unexpected prompt from the AWS CLI.** `aws configure` showed an optional "Select agents to configure" list for AI coding tools. I cancelled it and entered the credentials manually.
- **Occasional 500 errors from `/`.** The endpoint returned `500 We're down!` roughly 1 time in 10. I confirmed in `main.go` that this is intentional behaviour (a random roll), not a deployment problem. For this reason `/` should not be used as a load balancer health check.
- **Image architecture.** The Dockerfile downloads Go based on the build architecture, so I built with `--platform linux/amd64` to match Fargate.
