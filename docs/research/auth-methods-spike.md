# Auth Methods Research Spike

Status: initial research

Date: 2026-06-02

## Question

Agentic Mesh needs different authentication methods for different role agents,
worker adapters, and connectors. Project config must be able to choose an auth
method per role without storing credentials in Git.

## Sources Checked

- OpenAI Codex authentication and Codex access tokens.
- OpenAI API authentication.
- Anthropic API authentication.
- Claude Code authentication.
- DeepSeek API quickstart.
- MiniMax API prerequisites.
- Microsoft Graph authentication concepts.
- Azure OpenAI / Microsoft Entra authentication.

## Findings

### Codex CLI

Supported patterns:

- ChatGPT sign-in / OAuth cache for subscription or workspace-governed access.
- API key login for usage-based automation.
- Codex access token through `CODEX_ACCESS_TOKEN` for trusted non-interactive
  workflows in eligible ChatGPT workspaces.
- File-based `auth.json` under `CODEX_HOME` or OS credential store for cached
  login state.

Agentic Mesh auth methods:

- `codex_access_token`
- `codex_oauth_cache`
- `codex_api_key`

### OpenAI API

Supported pattern:

- API key sent as HTTP bearer auth.

Agentic Mesh auth methods:

- `openai_api_key`

### Azure OpenAI

Supported patterns:

- API key.
- Microsoft Entra ID or managed identity.

Agentic Mesh auth methods:

- `azure_openai_api_key`
- `azure_openai_entra_id`

### Anthropic API

Supported patterns:

- API key in `x-api-key` or `ANTHROPIC_API_KEY`.
- Workload Identity Federation for short-lived bearer tokens from a trusted
  identity provider.

Agentic Mesh auth methods:

- `anthropic_api_key`
- future: `anthropic_workload_identity_federation`

### Claude Code

Supported patterns:

- Cloud provider credentials for Bedrock, Vertex, or Foundry.
- `ANTHROPIC_AUTH_TOKEN` bearer token.
- `ANTHROPIC_API_KEY`.
- `apiKeyHelper` for dynamic or rotating credentials.
- `CLAUDE_CODE_OAUTH_TOKEN`.
- Subscription OAuth credentials from login.
- Credential cache directory under the Claude configuration directory.

Agentic Mesh auth methods:

- `anthropic_auth_token`
- `anthropic_api_key`
- `claude_code_oauth_token`
- `claude_code_oauth_cache`
- `claude_code_api_key_helper`
- `claude_code_bedrock`
- `claude_code_vertex`
- `claude_code_foundry`

### DeepSeek API

Supported pattern:

- API key sent as HTTP bearer auth to the DeepSeek endpoint.

Agentic Mesh auth method:

- `deepseek_api_key`

### MiniMax API

Supported pattern:

- API key obtained from the MiniMax platform.

Agentic Mesh auth method:

- `minimax_api_key`

### Microsoft Teams / Outlook Connectors

Microsoft Graph supports:

- Delegated access, where the app acts on behalf of a signed-in user.
- App-only access, where the app acts with its own identity using application
  permissions.
- App credentials such as client secret, certificate, or federated identity.

Agentic Mesh auth methods:

- `microsoft_graph_delegated_oauth`
- `microsoft_graph_app_client_secret`
- `microsoft_graph_app_certificate`
- `microsoft_graph_federated_identity`

## Product Decision

Represent auth as a configured binding:

```yaml
worker:
  adapter: codex-cli
  model: codex
  auth:
    method: codex_access_token
    secret_ref: codex-agentic-mesh-dev-product-token
```

The method comes from the system auth catalog in `config/auth-methods.yaml`.
Project config can choose a method per role agent, but it may only reference
secret names, mount names, or provider identity assumptions. It must not embed
secret values.

## Gaps

- Add Anthropic Workload Identity Federation as a first-class method once a
  concrete deployment target needs it.
- Decide how secret references resolve in the local profile.
- Decide how role-instance `CODEX_HOME` and Claude config volumes are created
  and refreshed across hibernation.
- Decide whether connector auth uses the same `AuthBinding` shape or a
  connector-specific extension with scopes and tenant metadata.

## Source Links

- [Codex authentication](https://developers.openai.com/codex/auth)
- [Codex access tokens](https://developers.openai.com/codex/enterprise/access-tokens)
- [OpenAI API authentication](https://developers.openai.com/api/reference/overview#authentication)
- [Anthropic API authentication](https://platform.claude.com/docs/en/manage-claude/authentication)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [DeepSeek API quickstart](https://api-docs.deepseek.com/)
- [MiniMax API prerequisites](https://platform.minimax.io/docs/guides/quickstart-preparation)
- [Microsoft Graph auth concepts](https://learn.microsoft.com/en-us/graph/auth/auth-concepts)
- [Azure OpenAI Microsoft Entra authentication](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/managed-identity)
