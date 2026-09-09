# OpenRouter for OSEE's read-only finance and tax chat

Verified against current official OpenRouter documentation on 7 September 2026. Scope: architecture notes for PT Langkah Pintar Nusantara; no credentials were accessed and no inference requests or private data were submitted.

## Recommended boundary

Use OpenRouter as a language-model gateway behind the ERP backend. The chat explains approved Indonesian tax sources and permission-filtered finance facts in plain Bahasa Indonesia. It cannot post journals, change tax treatment, release payments, submit SPT, alter master data or execute arbitrary SQL. The existing deterministic finance/tax engine remains the source of amounts and rules.

Flow: `signed-in finance user → ERP authorization → approved source retrieval/read-only fact tools → outbound minimization → OpenRouter → schema/citation/amount checks → answer with source links and effective dates`.

## Documented vendor behavior and its consequence

| Topic | Documented behavior | Proposed OSEE implementation |
|---|---|---|
| API | Chat Completions uses `POST https://openrouter.ai/api/v1/chat/completions`, Bearer authentication and a JSON request; the OpenAI-compatible base URL is `https://openrouter.ai/api/v1`. [Quickstart](https://openrouter.ai/docs/quickstart) | Call only from the backend. Store a dedicated inference key in the secret store; keep management credentials outside the chat service. |
| Tool calling | The model returns proposed function calls; application code executes them and sends results back. Tool support depends on the selected model/provider; tool definitions are included on each round. [Client tools](https://openrouter.ai/docs/guides/features/tool-calling) | Expose a small allowlist of authorized read functions. Validate tool name, schema and current user scope before execution; cap rounds and result sizes. |
| Structured output | `response_format.type = json_schema`, `strict: true` and `require_parameters: true` are supported for suitable endpoints. Actual strict enforcement varies by provider and exact compliance is not guaranteed everywhere. [Structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs) | Validate JSON, references and numeric claims after completion. A valid schema cannot prove a tax answer is correct. |
| Provider selection | `order` expresses preference; `only` restricts eligible providers; `allow_fallbacks` defaults to true. `require_parameters` defaults to false. `data_collection` defaults to allow; `zdr: true` restricts endpoint eligibility. [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection) | Pin approved model/provider combinations, require requested capabilities and enforce privacy settings on every round, including tool-result rounds. |
| Model fallback | The `models` array provides ordered fallback models. Documented triggers include provider failures, rate limits and moderation refusals. [Model fallbacks](https://openrouter.ai/docs/guides/routing/model-fallbacks) | Initially use one approved pair. If adding application-controlled alternates, use them only for permitted availability failures, with the same privacy and quality gates. Do not reroute to evade refusals or guardrails. |
| Guardrails | Model/provider allowlists intersect across applicable guardrails; ZDR is cumulative. A guardrail enforces nothing until assigned. Budgets are per user/key, not automatically one shared organization pool. [Guardrails](https://openrouter.ai/docs/guides/features/guardrails) | Assign the policy to the actual production key/workspace configuration, inspect effective eligibility and test rejection. A saved policy definition alone is insufficient. |

The primary/fallback model IDs should be selected by an Indonesian tax evaluation, then pinned in an approved configuration with review dates. Do not use unbounded auto routing, free-model routing or floating latest aliases for production tax answers. Detect capability or endpoint-policy changes and rerun approval tests before expanding eligibility.

## Privacy and retention are separate controls

`data_collection: "deny"` and `zdr: true` should both be set. Do not interpret a no-training setting as zero retention: the dedicated ZDR documentation distinguishes providers that do not train but still retain data. Policies are endpoint-specific, not merely provider-brand-specific. OpenRouter's ZDR classification also permits implicit in-memory prompt caching. Its published ZDR endpoint inventory can change. [ZDR documentation](https://openrouter.ai/docs/guides/features/zdr)

OpenRouter states prompt/response storage is opt-in, but it retains request metadata and describes anonymous categorization of sampled prompts using a ZDR model. This is not a promise that no processing or metadata exists anywhere. Keep both private input/output logging and the separate use-of-inputs/outputs opt-in disabled. [Data collection](https://openrouter.ai/docs/guides/privacy/data-collection)

If private input/output logging is enabled, the documentation specifies a minimum three-month retention with possible longer retention unless deletion is requested. Broadcast is a separate path to external observability systems. Keep both off for this finance chat unless separately reviewed and approved. [Input/output logging](https://openrouter.ai/docs/guides/features/input-output-logging)

Proposed controls:

- Remove NPWP/NIK, bank account numbers, credentials, participant contact data and unnecessary names before external inference. Pseudonymize internal parties and send the minimum fact subset needed to answer. Keep pseudonym mappings inside the ERP.
- Scan every outbound message, retrieved excerpt and tool result in the ERP. Do not assume provider filtering alone covers all content or authorization risks.
- Keep original tax documents, student records and bank files in ERP-controlled storage. Start with text excerpts; do not enable hosted file storage, shell tools, arbitrary URL fetch, web-search plugins or other server tools for this chat.
- Record the user's access scope, source versions, tool/result IDs, prompt-template/configuration versions, generation ID, selected model/provider, validation status and usage. Store any retained conversational content under an explicit internal retention policy, separate from immutable accounting evidence.
- Confirm applicable contractual terms, processing locations and endpoint policies. ZDR is not evidence of Indonesian data residency or a complete compliance assessment.

Documentation caveat: the generic Provider Logging page contains an older-looking statement that retention does not affect routing, while the dedicated ZDR and routing pages explicitly describe ZDR enforcement. Use the dedicated current ZDR configuration, verify effective endpoint eligibility, and resolve contractual uncertainty before sending sensitive production content. [Provider Logging](https://openrouter.ai/docs/guides/privacy/provider-logging)

## Minimal request policy

Illustrative raw-HTTP fields, with deployment-specific placeholders; this is not a runnable credentialed example:

```json
{
  "model": "<approved-pinned-model-id>",
  "messages": [
    {"role": "system", "content": "<approved system instructions>"},
    {"role": "user", "content": "<minimized authorized question and context>"}
  ],
  "provider": {
    "only": ["<approved-provider-endpoint-slug>"],
    "order": ["<approved-provider-endpoint-slug>"],
    "allow_fallbacks": false,
    "require_parameters": true,
    "data_collection": "deny",
    "zdr": true
  },
  "max_tokens": 1800,
  "stream": false,
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "osee_tax_answer_minimal",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "answer_bahasa_indonesia": {"type": "string"},
          "source_ids": {"type": "array", "items": {"type": "string"}},
          "review_required": {"type": "boolean"}
        },
        "required": ["answer_bahasa_indonesia", "source_ids", "review_required"],
        "additionalProperties": false
      }
    }
  }
}
```

The token cap is a proposed starting limit to evaluate. Prefer buffering and validating a complete answer before display; show an accessible progress state while working. In a multi-round tool flow, request tool calls first and apply the structured final-answer contract on the answer stage, verifying support for the actual parameter combination.

The canonical implementation contract is section 12.6 of the main architecture. In this minimal example, any model-proposed `review_required` value is advisory only: the backend enforces missing-fact/review gates and typed eligibility, rate, deadline and payment/filing states. A model cannot grant approval or suppress a required review by returning `false`.

## Read-only tools and answer contract

Proposed tools: `search_approved_tax_sources(query, tax_period)`, `get_entity_tax_profile_snapshot(period)`, `get_tax_workpaper_summary(period, tax_type)`, `get_invoice_settlement_explanation(invoice_id)` and `get_close_readiness(period)`. Use typed, parameterized backend queries and restricted database credentials. Derive entity/user permissions from the authenticated session, never from model-provided IDs. Read-only tools can still leak data, so enforce the same permissions as normal ERP screens and restrict cross-mitra visibility.

Retrieve official DJP/JDIH/legal sources with title, URL, article/section, effective interval, retrieval/review date and approved interpretation. Separate law, official guidance and OSEE policy. Historical questions use rules effective for that period; current questions require sufficiently recent source review. Missing facts, conflicting rules or stale evidence should produce a specific question or an explanation of what needs review, not an unsupported confident conclusion.

The production answer contract extends the minimal schema above with `answer_id`, `answer_bahasa_indonesia`, `as_of_date`, `tax_period`, `source_ids`, `source_locations`, `erp_fact_refs`, `assumptions`, `missing_facts`, `review_required`, and `suggested_next_screen`. The backend resolves source IDs to trusted links; the model cannot invent clickable official citations. Numeric explanations must reconcile to returned deterministic workpaper facts. Suggested next screens are links, not executable finance commands.

Evaluate with PPh 23 versus PP 23/PP 55 confusion; withholding receivable versus payable; paid tax versus accepted SPT; historical rule changes; reseller deposit versus revenue; variable provider cost; missing bukti potong; cross-mitra access attempts; prompt injection embedded in a retrieved document; fabricated citations; and inaccessible/stale source cases. All alternates must pass the same evaluation.

## Cost and failure behavior

API keys support a USD spending limit, optional daily/weekly/monthly reset and expiry; documented resets use UTC boundaries. BYOK usage has a separate include-in-limit option. [API key creation schema](https://openrouter.ai/docs/api/api-reference/api-keys/create-a-new-api-key) Usage responses include tokens, cost and available reasoning/cache information; streaming usage arrives at the end. [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)

Workspace budgets can provide a shared spend boundary where available. Checks happen before dispatch: in-flight requests may complete and exceed the nominal budget. BYOK inclusion must be configured if used. [Workspace budgets](https://openrouter.ai/docs/guides/features/workspaces/workspace-budgets)

Therefore add ERP per-user and global quotas, context/output/tool-round caps, concurrent-request limits and a pessimistic reservation before dispatch; reconcile reserved versus actual usage afterward. Track unknown cost for interrupted requests instead of treating missing usage as zero. Keep a separate operational budget alert and do not let chat exhaustion block accounting or filing workflows. Prices and plan entitlements must be checked at implementation time rather than hard-coded into this design.

HTTP 200 alone is not success: documented completion failures can appear inside a nonstreaming JSON response or SSE event after headers are sent. Fallback stops after answer content starts reaching the application. Check error envelopes, terminal state and completeness; respect documented `Retry-After` behavior. [Errors and debugging](https://openrouter.ai/docs/api_reference/errors-and-debugging)

Proposed failure policy: limited jittered retries for eligible transient failures; no retry loop for invalid keys, policy rejection, bad schema or exhausted budget. Never relax the allowlist/ZDR policy for availability. If no approved endpoint works, return a short Bahasa Indonesia message and links to the retrieved source/workpaper. Never label truncated, malformed or unvalidated output a completed tax answer. Cancellation or a failed response is not assumed to mean no provider cost.
