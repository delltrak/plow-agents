# plow-agents

Build an agent image, run it on Plow, and text it from your phone.

## What you need

Python 3.11 or newer, Git, Docker, a registry account that can publish public images,
and a phone that can send an activation text. For local runs, use Docker Compose
2.24 or newer and an agent repository with a Dockerfile and `compose.yml`.

Your agent reads `PLOW_API_BASE` from its environment; if `PLOW_AGENT_TOKEN` is set,
send it as a bearer. See the [image contract](https://github.com/plow-pbc/plow/blob/main/api/cloud-agents/README.md).

## 1. Install and log in

```sh
git clone https://github.com/plow-pbc/plow-agents.git
export PATH="$PWD/plow-agents/bin:$PATH"
plow-agents login
plow-agents lines
```

Text the activation phrase to the number printed by `login`. The `lines` command
shows the global pool: any account may claim an unheld line. Keep the ID of a
`free` line for step 5. A line held by another account is `in use`; your own
agent is shown by its uid. An older API that does not report availability shows
`unknown` instead of claiming that a line is free.

## 2. Write a Dockerfile

Start with a Dockerfile for your agent. For example, if your Python agent starts
from `agent.py`:

```dockerfile
FROM python:3.11-slim
COPY agent.py /agent.py
CMD ["python", "/agent.py"]
```

Keep credentials out of Git and image builds:

```sh
printf '\n/plow-credentials\n' >> .gitignore
printf '\n/plow-credentials\n' >> .dockerignore
```

## 3. Build the image

```sh
plow-agents image build ghcr.io/YOUR_ACCOUNT/my-agent:v1
```

If ./plow-agents.toml has image = "…", you can omit the name.

## 4. Push the image

Create a [classic GitHub PAT](https://github.com/settings/tokens/new?scopes=write:packages) with `write:packages`; fine-grained PATs cannot push to GHCR. Use the PAT as the password when Docker prompts:

```sh
docker login ghcr.io -u YOUR_GITHUB_USERNAME
plow-agents image push ghcr.io/YOUR_ACCOUNT/my-agent:v1
```

After the first push, make the package public in GitHub package settings; otherwise Plow’s anonymous pull fails.
Copy the full `repository@sha256:…` reference printed on the last line.

## 5. Request an agent

Replace `ln_xxx` with the free line ID from step 1 and use the reference from step 4:

```sh
plow-agents deploy ghcr.io/YOUR_ACCOUNT/my-agent@sha256:… --line ln_xxx
plow-agents agents
```

You can also deploy a listing with `plow-agents deploy exe:hermes --line ln_xxx`.

## 6. Text it

Run `plow-agents agents` until the status is `running`. Text the number shown by
`plow-agents lines`. A `failed` status includes the failure code beside it.

## 7. Run it locally

Copy [compose.example.yml](compose.example.yml) into your agent repository as
`compose.yml`, then choose a free line:

```sh
curl -fsSL https://raw.githubusercontent.com/plow-pbc/plow-agents/main/compose.example.yml -o compose.yml
plow-agents lines
plow-agents deploy --local --line ln_xxx
docker compose logs -f
```

If the container reaches your API at a different address, pass
`--agent-api-base URL` to `plow-agents deploy --local`. When finished:

```sh
plow-agents revoke
docker compose down -v
```

## Leaderboard

1. Build on this template and the [plow-hermes-agent base](https://github.com/plow-pbc/plow-hermes-agent). See [life-assistant-hermes-agent](https://github.com/plow-pbc/life-assistant-hermes-agent) for a working example.
2. Pick an ID and register it once from your agent checkout, using the existing `./plow-credentials`:

```sh
curl -O https://raw.githubusercontent.com/plow-pbc/agent-index-client/f900ff144076f0a766584b6ec4d0993600779b16/standalone/agent_index_client.py
set -a; . ./plow-credentials; set +a
python3 agent_index_client.py --register --agent "<your-agent-id>" --name "<Agent name>" --blurb "<one line>"
```

3. The reporter runs as an s6 longrun in your image; see [Building a variant image](https://github.com/plow-pbc/plow-hermes-agent/blob/main/README.md#building-a-variant-image) for how to add one. Copy the [life-assistant agent-index service](https://github.com/plow-pbc/life-assistant-hermes-agent/tree/main/image/s6-overlay/s6-rc.d/agent-index) and its [client installation](https://github.com/plow-pbc/life-assistant-hermes-agent/blob/main/Dockerfile).

Add `AGENT_ID` under `environment` in your agent service in `compose.yml`, set your registered ID, then rebuild and start it. Reports appear on the [leaderboard](https://aiworthusing.com/agent-index).

`profile` sets your account's public name and photo. `--photo` takes a local file Plow hosts, or a public HTTPS URL.

```sh
plow-agents profile --name "Ada" --photo ./ada.png
plow-agents profile --name "Ada" --photo https://example.com/ada.jpg
plow-agents profile --show
```

## Commands

| Command | Purpose |
| --- | --- |
| `plow-agents login` | Log in by text. |
| `plow-agents lines` | Show the global pool of line IDs, numbers, and availability. |
| `plow-agents profile [--name NAME] [--photo PHOTO] [--show]` | Set or show your profile; a photo can be a file or HTTPS URL. |
| `plow-agents mint LINE [--credential-file PATH] [--agent-api-base URL]` | Write a credential for a self-hosted agent. |
| `plow-agents rotate [--credential-file PATH]` | Replace the credential; recreate the container to load it. |
| `plow-agents revoke [LINE] [--credential-file PATH]` | Retire any agent on LINE, or the credential-file self-hosted agent. |
| `plow-agents image build [IMAGE]` | Build the current directory for linux/amd64. |
| `plow-agents image push [IMAGE]` | Push and print the full image reference. |
| `plow-agents deploy TARGET --line LINE` | Request an image@sha256:… or an `exe:slug` listing. |
| `plow-agents deploy --local --line LINE [--agent-api-base URL]` | Mint a credential and start Compose locally. |
| `plow-agents agents` | Show tab-separated line, target, and status. |

Every command accepts `--help`. Global `--api-base URL` and `--token-file PATH`
options go before the command.

## Where changes go

Before editing, find the owner in the [sibling repo map](https://github.com/plow-pbc/plow-hermes-agent/blob/main/README.md#the-repos) and make the change there.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.
"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
