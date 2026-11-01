# Deploying Nebula on AWS (CDK)

> Estimate the monthly cost before you deploy (see "Rough monthly cost" below), and check that the names below (`Nebula`, `nebula/app`) don't clash with anything else in your account.

One CDK stack, `Nebula`, deployed to the account and region of your AWS credentials (us-east-2 unless you set `NEBULA_AWS_REGION`). It's a separate uv project, so the CDK dependencies stay out of the app's environment. The CLI runs through `npx aws-cdk@2` and the app command is `uv run python app.py` (see `cdk.json`).

```
Claude ──HTTPS──► CloudFront (*.cloudfront.net, PriceClass_200)
                      │ HTTP + X-Nebula-Origin header
                      ▼
                  ALB :80  (ingress only from the CloudFront prefix list; 403 unless the header matches)
                      │
                      ▼
       ECS Fargate ARM64, public subnets, public IPs (no NAT)
          nebula-api    (nebula serve, :8000)  ──┐
          nebula-worker (nebula worker)        ──┼──► Aurora PostgreSQL 17 Serverless v2 (isolated subnets)
                                                 └──► Internet: Google, Bedrock, ECR, Secrets Manager
```

## Files

| File | What it is |
|---|---|
| `app.py` | Entry point. Pins account/region and tags everything `project=nebula`. |
| `nebula_stack.py` | Every resource. Sizes, versions and the Docker exclude list are constants at the top. |
| `cdk.json` | CLI config, feature flags, and the plain-text app settings under `nebula:*`. |
| `cdk.context.json` | Cached lookups (AZs, the CloudFront prefix list id `pl-b6a144df`). Commit it so synth stays deterministic. |
| `../Dockerfile`, `../.dockerignore` | The single image both services run. |

## Prerequisites

- AWS CLI signed in to the account you're deploying to (`export AWS_PROFILE=<your profile>`)
- Node (for `npx aws-cdk@2`), uv, and Docker Desktop running (the deploy builds the ARM64 image locally)
- For ECS Exec, the Session Manager plugin: `brew install --cask session-manager-plugin`

## Bootstrap and deploy

```sh
cd infra
export AWS_PROFILE=<your profile>

# Once per account/region: creates the CDK toolkit stack, asset bucket and ECR repo
npx aws-cdk@2 bootstrap aws://<account-id>/<region>

npx aws-cdk@2 synth          # sanity check, no AWS changes
npx aws-cdk@2 diff Nebula    # what would change
npx aws-cdk@2 deploy Nebula  # builds the image (with models), pushes to ECR, deploys
```

The first deploy takes about 20–30 minutes. Aurora and CloudFront are the slow parts, and the image push is around 3.5 GB because the models are baked in. Later deploys that only touch `src/` rebuild just the top layer.

Once the first deploy finishes:

1. Set the real values in the `nebula/app` secret (see below).
2. Add the `GoogleRedirectUri` output to the Google OAuth client's authorised redirect URIs.
3. Restart both services so they pick up the new secret values.

The stack outputs `McpUrl`, which is the address people paste into Claude.

### App settings that aren't secret

These live in `cdk.json` under `context` and become environment variables. You can override any of them for a single deploy with `-c`:

| Context key | Env var | Default |
|---|---|---|
| `nebula:publicSignup` | `NEBULA_PUBLIC_SIGNUP` | `true` in this deployment; the app and stack fallback default is `false` |
| `nebula:allowedEmailDomains` | `NEBULA_ALLOWED_EMAIL_DOMAINS` | `example.org` |
| `nebula:allowedEmails` | `NEBULA_ALLOWED_EMAILS` | empty (left unset) |
| `nebula:adminEmails` | `NEBULA_ADMIN_EMAILS` | empty (left unset) |
| `nebula:embedProvider` | `NEBULA_EMBED_PROVIDER` | `local` (Bedrock is blocked on this account for now) |
| `nebula:rerankProvider` | `NEBULA_RERANK_PROVIDER` | `local` |

```sh
npx aws-cdk@2 deploy Nebula -c nebula:adminEmails=you@example.org
```

The stack also sets `NEBULA_ENV=prod`, `NEBULA_AUTH_MODE=google`, `NEBULA_AWS_REGION`, `NEBULA_DB_NAME=nebula` and `NEBULA_PUBLIC_URL` (the `https://…cloudfront.net` address with no trailing slash). The image sets `FASTEMBED_CACHE_PATH=/opt/models`.

Public sign-in does not make an account an RL member. The domains in `allowedEmailDomains`
define implicit RL membership; an administrator can also add a person to `all_rl` explicitly.
Keep that list restricted to the organisation even when `publicSignup=true`. Setting
`publicSignup=false` restores the email/domain sign-in allowlist; it does not erase stored data.

## Resources and what they're for

| Resource | Why |
|---|---|
| VPC `nebula` (10.40.0.0/16) | Two AZs, each with a public and an isolated subnet, and no NAT gateway. The S3 gateway endpoint is free, and it keeps container image layers (which ECR stores in S3) off the internet. |
| Security groups | The ALB takes port 80 only from `com.amazonaws.global.cloudfront.origin-facing`. Tasks take 8000 only from the ALB. Aurora takes 5432 only from the tasks. |
| Aurora PostgreSQL 17.11 Serverless v2 (`nebula`) | One writer scaling between 0.5 and 4 ACU, encrypted, 7-day backups, and a final snapshot if the stack is deleted. The app runs `CREATE EXTENSION vector` itself. |
| Secret `nebula/db` | Generated DB credentials. ECS injects host, port, username and password from it. |
| Secret `nebula/app` | Google OAuth client and the service account JSON, both starting as `"unset"`. `JWT_SIGNING_KEY` is generated (64 alphanumeric characters). The secret is retained if the stack is deleted. |
| Secret `nebula/origin-verify` | The `X-Nebula-Origin` value. CloudFront sends it and the ALB rule requires it, so only this distribution can reach the ALB. |
| ECS cluster `nebula` | Fargate on ARM64 (Graviton). |
| Service `nebula-api` | Runs `nebula serve` on 1 vCPU / 6 GB. Autoscales from 1 to 3 tasks at 60% CPU. Container health check on `/healthz`, 180 s grace period, circuit breaker with rollback, ECS Exec on. |
| Service `nebula-worker` | Runs `nebula worker` on 1 vCPU / 6 GB, with 1 task, no load balancer, circuit breaker with rollback and ECS Exec on. |
| Log groups `/nebula/api`, `/nebula/worker` | Kept for 1 month. |
| ALB | Internet-facing with a 120 s idle timeout. HTTP:80 returns 403 by default and forwards to the api target group only when the header matches. The target group has a `/healthz` check and a 30 s deregistration delay. |
| CloudFront | Default `*.cloudfront.net` certificate, HTTP/2 and HTTP/3, redirects to HTTPS, all methods allowed, caching off, all viewer headers including Host and Authorization forwarded, compression on, PriceClass_200 (includes India). |
| IAM task role | Shared by both services. It can call `bedrock:InvokeModel*`, `Rerank` and `GetInferenceProfile` on `*`, read `nebula/app`, and use ECS Exec. |
| IAM execution role | ECR pull, log writes, and reading the two injected secrets. |

## Rough monthly cost (us-east-2)

This is the historical 4 GB-per-task estimate, not a current quote. Production uses 6 GB
per task for the local models. Check AWS Billing or refresh the calculator before relying
on the table below.

These are on-demand list prices, assuming 730 hours a month, light internal use by one org, and no free-tier credits except CloudFront's always-free tier. Check them in the AWS Pricing Calculator before relying on the numbers.

| Item | Assumption | ≈ USD/month |
|---|---|---|
| Fargate api | 1 task, ARM64 1 vCPU / 4 GB ($0.03238/vCPU-h, $0.00356/GB-h) | 34 (up to 102 at 3 tasks) |
| Fargate worker | 1 task, same size | 34 |
| Aurora Serverless v2 | 0.5 ACU floor at $0.12/ACU-h. It's about $88 if it averages 1 ACU. | 44 |
| Aurora storage + I/O | 10 GB at $0.10, about 5 M I/Os at $0.20/M. Backups within the 7-day window are free. | 2 |
| ALB | $0.0225/h plus a fraction of an LCU | 20 |
| Public IPv4 | 2 task IPs + 2 ALB IPs at $0.005/h each | 15 |
| CloudFront | Inside the always-free 1 TB / 10 M requests | 0 |
| Secrets Manager | 3 secrets at $0.40 | 1 |
| CloudWatch Logs | About 5 GB ingested at $0.50/GB | 3 |
| ECR (CDK assets repo) | About 3.5 GB image; unchanged layers are shared between versions | 1 |
| VPC | No NAT; the S3 gateway endpoint is free | 0 |
| **Total** | | **≈ $153 at idle, ≈ $198 with the DB around 1 ACU** |

The two biggest levers are the database floor and task size. Dropping the min ACU to 0 lets the database auto-pause, but the first query after a pause waits about 15 s. Shrinking tasks is only an option once Bedrock replaces the local models.

## Setting the app secret

The secret is one JSON object, so read it, merge your changes with `jq`, and write it back. Piping keeps the values off disk.

```sh
export AWS_PROFILE=<your profile> AWS_REGION=us-east-2

aws secretsmanager get-secret-value --secret-id nebula/app --query SecretString --output text \
| jq --arg id  "<google-oauth-client-id>" \
     --arg sec "<google-oauth-client-secret>" \
     --rawfile sa ./service-account.json \
     '.GOOGLE_CLIENT_ID=$id | .GOOGLE_CLIENT_SECRET=$sec | .GOOGLE_SERVICE_ACCOUNT_JSON=$sa' \
| aws secretsmanager put-secret-value --secret-id nebula/app --secret-string file:///dev/stdin

# ECS injects secrets at task start, so restart both services afterwards
aws ecs update-service --cluster nebula --service nebula-api    --force-new-deployment >/dev/null
aws ecs update-service --cluster nebula --service nebula-worker --force-new-deployment >/dev/null
```

To set just one field, drop the other `--arg`s and assignments. Leave `JWT_SIGNING_KEY` alone, because changing it signs everyone out.

**Don't edit the secret's template in `nebula_stack.py` after the first deploy.** If `GenerateSecretString` changes, CloudFormation regenerates the whole secret and wipes the values you set by hand. To add a new field, add it with the CLI as above. Then add a matching `ecs.Secret` in the stack, outside the template.

## Running a one-off command in the api container (ECS Exec)

```sh
export AWS_PROFILE=<your profile> AWS_REGION=us-east-2
TASK=$(aws ecs list-tasks --cluster nebula --service-name nebula-api --query 'taskArns[0]' --output text)

# A single command
aws ecs execute-command --cluster nebula --task "$TASK" --container api \
  --interactive --command "nebula --help"

# Or a shell. The image runs as the non-root user `nebula`, and its environment includes the injected secrets.
aws ecs execute-command --cluster nebula --task "$TASK" --container api --interactive --command "/bin/sh"
```

For the worker, use `--service-name nebula-worker` and `--container worker`. Exec needs the Session Manager plugin installed locally. It only works on tasks started after ECS Exec was enabled, which covers every task this stack starts.

## Logs and status

```sh
aws logs tail /nebula/api --follow
aws logs tail /nebula/worker --follow
aws ecs describe-services --cluster nebula --services nebula-api nebula-worker \
  --query 'services[].{name:serviceName,running:runningCount,desired:desiredCount,rollout:deployments[0].rolloutState}'
```

## Things to know

- **Aurora version.** 17.11 is the newest 17.x in us-east-2. The CloudFormation schema bundled with aws-cdk-lib 2.273 only lists versions up to 17.10, so `nebula_stack.py` acknowledges that one synth-time check (`CloudFormation-Validate::E9006`). If the deploy ever rejects 17.11, set `AURORA_PG_FULL_VERSION = "17.10"`.
- **CloudFront origin read timeout is 60 s.** This is the gap allowed between bytes from the ALB. A tool call or SSE stream that goes quiet for longer gets a 504, so the app should stream or ping within 60 s. AWS will raise the limit to 180 s on a quota request.
- **Memory.** The baked models are about 2.8 GB on disk: multilingual-e5-large is 2.24 GB and the int8 reranker is 0.57 GB. If tasks get OOM-killed once both models are loaded, raise `TASK_MEMORY_MIB` to 6144 or 8192.
- **No custom domain yet.** Adding one later means an ACM certificate in us-east-1, plus `domain_names` and `certificate` on the distribution. Then update `NEBULA_PUBLIC_URL` and the Google redirect URI.
- **Deleting the stack** snapshots Aurora and keeps `nebula/app`. Log groups are deleted with it.
