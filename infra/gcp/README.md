# GCP deployment

The personal public project `weaves-510819` now uses the
[single-VM deployment](singlevm/README.md): PostgreSQL, API, worker, frontend and
Caddy run on one `e2-medium` VM. [Current public URLs and DNS](PUBLIC.md).
Cloud SQL, Cloud Run and the load balancer were retired for this project.

## Legacy managed profile

The managed profile below uses `asia-south1` with a Cloud Run API, a one-instance
Cloud Run worker pool, and a Cloud Run frontend. The bootstrap profile below
provisions regional high-availability PostgreSQL. It is retained for other
projects and must not be used to redeploy the personal public environment.
Runtime passwords, the database URL and encryption key are generated
directly into Secret Manager. They do not belong in repository files or GitHub settings.

## First deployment

Prerequisites: authenticated `gcloud`, `gh` authenticated to both repositories,
Docker, Python 3, OpenSSL, and permission to configure project IAM and billing.

From the backend repository root, run:

```sh
bash infra/gcp/bootstrap.sh
```

This creates the Cloud SQL instance, Artifact Registry repository, runtime and
deployment identities, Secret Manager secrets, and GitHub OIDC trust. It writes
only nonsecret GCP identifiers as GitHub Actions repository variables. GitHub
Actions authenticates through Workload Identity Federation; no service-account
key is created.

For the first deploy, clone `weavelabs-pvt-ltd/weaves-app` beside this repository as
`../weaves-app`, then run:

```sh
bash infra/gcp/deploy-initial.sh
```

The script builds and pushes both containers, starts the frontend and API,
starts the background worker, and runs the API smoke test when
`WEAVES_E2E_TOKEN` is set. The first account owner is created out of band with
`infra/gcp/singlevm/bootstrap_account_owner.py` from a host with database owner
access; there is no public signup.

The managed bootstrap history below is retained for reference only. Current
GitHub workflows deploy automatically to the personal single VM after quality
checks pass on every push to main. See [current CI/CD](singlevm/README.md#automatic-deployment).
They do not deploy Cloud Run or use the old CI owner secrets.

## URLs and domains

The first deploy prints the Cloud Run `run.app` URLs for the UI and API. Custom
domain routing is intentionally a separate step because it requires editing
the domains' DNS records. The intended production hosts are
`app.weave-studio.in` and `api.weave-studio.in`.
