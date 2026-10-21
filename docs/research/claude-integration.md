# Calling Claude Opus 5.5 as final decision maker from a self-hosted FastAPI app: subscription vs API key

Research date: 2026-10-20. Every source was fetched and read, not taken from snippets. **[INFERENCE]** is my reading. **[UNVERIFIED]** is something I could not confirm from an official page.

## Bottom line

- Use an **Anthropic API key** (Console, Commercial Terms) as the primary path.
- Estimated cost is about **$0.12–0.35 per decision**, or **$3–21 per month** at 1–3 decisions per trading day.
- The subscription path (setup-token plus Agent SDK or `claude -p`) works technically but is a grey area under the terms.
- The Consumer Terms contain a clause that excludes relying on the service "to buy or sell securities", and a clause against automated access. The Commercial Terms have neither, as far as I could find.

## 1. Technically possible paths

| Path | Mechanism | Credential | Verdict |
|---|---|---|---|
| A. API key | Messages API via `anthropic` Python SDK, model `claude-opus-5-5` | Console `ANTHROPIC_API_KEY`, pay per token | Cleanest |
| B. Agent SDK, subscription | `claude-agent-sdk` bundles a native Claude Code binary and spawns it | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` | Works; terms grey area |
| C. Headless CLI, subscription | `claude -p "<prompt>" --output-format json [--json-schema ...]` subprocess | Same token | Same as B, less structure |

Supporting facts:

- The Agent SDK "gives you the same tools, agent loop, and context management that power Claude Code". The Python and TS packages bundle the binary. Sources: *Run Claude Code programmatically* (https://code.claude.com/docs/en/headless, 2026); *Agent SDK quickstart* (https://code.claude.com/docs/en/agent-sdk/quickstart, 2026).
- `claude setup-token` prints a one-year OAuth token and does not store it. You export it as `CLAUDE_CODE_OAUTH_TOKEN`. The docs say it is for "CI pipelines, scripts, or other environments where interactive browser login isn't available". It needs a Pro, Max, Team or Enterprise plan and "can only make model requests" (no Remote Control, no connectors). `--bare` mode does **not** read this token. Source: *Authentication* (https://code.claude.com/docs/en/authentication, 2026).
- Credential precedence is: cloud provider, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`, profiles, then `/login`. A stray `ANTHROPIC_API_KEY` silently wins over the subscription and bills the API. Sources: *Authentication*; *Use Claude Code with your Pro or Max plan* (https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan, 2026-10-14).
- Non-interactive controls: `--permission-mode dontAsk` denies every call that would otherwise prompt, but reads inside the working directory still run. `--output-format json` and `--json-schema` exist. Source: *headless* page.
- SDK structured output: pass a draft-07 JSON Schema as `output_format`. Treat a result as success only if `subtype == "success"` **and** `structured_output` is present. Source: *Structured outputs* (https://code.claude.com/docs/en/agent-sdk/structured-outputs, 2026).
- `claude-opus-5-5` exists with a 1M context window. `claude-fable-5-1` sits above it at $10/$50 per MTok. Sources: *Models overview* (https://platform.claude.com/docs/en/about-claude/models/overview, 2026); *Pricing* (https://platform.claude.com/docs/en/about-claude/pricing, 2026).
- Claude Code's cost docs say thinking cannot be turned off on Opus 5.5 and bills as output. I did not verify the raw Messages API behaviour, so budget for thinking on every path. Source: *Manage costs effectively* (https://code.claude.com/docs/en/costs, 2026).

## 2. What the terms allow

**Binding documents.** A personal subscriber is bound by the Consumer Terms (for an EEA consumer, Anthropic Ireland) and the Usage Policy (AUP). Source: *Consumer Terms of Service* (https://www.anthropic.com/legal/consumer-terms; the fetched copy was dated 2025-10-08, so check for revisions). I re-confirmed the clauses below with a live text probe.

1. **§3(7)** bans "access to the Services through automated or non-human means, whether through a bot, script, or otherwise", *except* "when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it".
2. **§3(9)** bans using the services "to buy or sell securities or to provide or receive advice about securities, commodities, derivatives, or other financial products or services, as Anthropic is not a broker-dealer or a registered investment adviser".
3. **§2** bans sharing account login information and API keys.
4. **§4** says outputs "may not always be accurate" and should be independently confirmed.

**Claude Code docs.** *Legal and compliance* (https://code.claude.com/docs/en/legal-and-compliance, 2026) says:

- OAuth "is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native Anthropic applications".
- "Developers building products or services that interact with Claude's capabilities, including those using the Agent SDK, should use API key authentication".
- Anthropic does not permit offering Claude.ai login to others, routing requests through Free/Pro/Max credentials "on behalf of their users", or collecting or intermediating Claude.ai credentials.
- "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."
- Enforcement may happen "without prior notice".

*Agent SDK overview* (https://code.claude.com/docs/en/agent-sdk/overview, 2026) repeats the third-party login point and says SDK use is governed by the Commercial Terms.

**AUP.** Source: *Usage Policy* (https://www.anthropic.com/legal/aup; the page header showed "2026-11-12", later than today, so treat that date as unreliable).

- "Do Not Abuse Our Platform" bans "services that route requests through consumer subscriptions or misrepresent the product or client being used".
- The Universal Usage Standards I retrieved contain **no financial-advice prohibition**.
- The page names "High-risk Use Case Requirements" and "Additional Use Case Guidelines (agentic use)", but their text was **not retrievable**. [UNVERIFIED] whether finance is a listed high-risk use case.

**Commercial Terms** (https://www.anthropic.com/legal/commercial-terms, 2026). A probe of about 24.9k characters found no securities, broker-dealer, investment-adviser or "non-human means" clause. It says "Anthropic may not train models on Customer Content from Services". The Consumer Terms allow training on your Materials unless you opt out.

**My reading [INFERENCE].**

- **Subscription path.**
  - A single-user script running the unmodified Claude Code binary with your own token is not one of the explicitly prohibited acts. The setup-token docs describe scripted use as the purpose, which plausibly counts as "explicitly permit".
  - "Should use API key" is a steer, not a clear ban, for a personal tool.
  - §3(9) is the sharp edge. A system whose output drives buy/sell decisions sits close to it, even on a paper DEMO account, because the goal is buy/sell forecasts benchmarked against the owner's real holdings.
  - The enforcement risk is throttling or suspension of the owner's entire Claude account.
- **API path.** The Consumer Terms, Claude Code docs and SDK docs all point developers here, and the Commercial Terms lack the two clauses. Keep outputs for the owner's own research, never advice to third parties.
- **Business-purpose wording.** The Commercial Terms speak of "Customers". How they treat an individual self-hosting a personal tool is [UNVERIFIED].

## 3. Limits and cost per decision

### API key path

Source for rates: *Pricing* page.

| Item | Opus 5.5 rate |
|---|---|
| Input | $4 / MTok |
| Output (thinking counts) | $20 / MTok |
| Cache write 5-minute / 1-hour | $5 / $8 / MTok |
| Cache read | $0.20 / MTok |
| Batch API (async) | $2 in / $10 out |
| Fast mode | $8 in / $40 out |

Models from Opus 4.7 onward use a tokenizer producing about 30% more tokens for the same text. If the digest was sized with another tokenizer, measure with the token-counting endpoint.

**Estimate per decision [INFERENCE].** Visible output is about 1k tokens. The thinking figure is my assumption, not a measurement.

| Scenario | Input | Output (visible + thinking) | Cost |
|---|---|---|---|
| Low | 15k | 1k + 2k | **$0.12** |
| Mid | 22k | 1k + 4k | **$0.19** |
| High | 30k | 1k + 10k | **$0.34** |

The High case rises by about $0.04 if the 30% tokenizer uplift applies to the input.

| Frequency (21 trading days) | Low | High |
|---|---|---|
| 1 decision/day | ~$2.5 | ~$7.1 |
| 3 decisions/day | ~$7.6 | ~$21.4 |

- Caching helps little because each digest is unique. Only a static system prompt is cacheable.
- Batch is half price but asynchronous, so it fits an end-of-day review, not intraday decisions.

**Limits.** Source: *Rate limits* (https://platform.claude.com/docs/en/api/rate-limits, 2026).

- Limits depend on organization usage tier, and new organizations may start in a lower Evaluation tier.
- The table I read listed Opus 5.5 at 1,000 RPM, 2,000,000 ITPM and 400,000 OTPM. A few calls a day is far below that.
- Monthly spend caps are $500 (Start), $1,000 (Build) and $200,000 (Scale).
- You can set your own spend limit on a **non-default Workspace**. Hitting it returns HTTP 400 `invalid_request_error`.
- Hitting the tier cap returns 429 `rate_limit_error` with `error.details.error_code = "enforced_spend_limit_reached"` and **no** `retry-after`.

**Max plan credits.** The pricing page lists "$100 to $200/month in API credits to build on the Claude Platform" under Max (*Plans & pricing*, https://claude.com/pricing, 2026). The Help Center says these credits "can't be used in Claude Code or the Claude apps". If they can fund an API key, the API path may cost the owner nothing out of pocket. Their terms (expiry, rollover, eligible models) are [UNVERIFIED].

### Subscription path

- Marginal cost is $0, but usage shares limits with everything else the owner does in Claude and Claude Code. Pro and Max have a **five-hour session limit and a weekly limit**, and Max has a separate weekly Fable limit. A small "wrap-up allowance" can finish an in-progress response but not start a new one. Source: Help Center article, 2026-10-14.
- Pro is $20/month ($17 annual). Max is from $100/month (5x and 20x). Source: *Plans & pricing*.
- Numeric allowances are not published on the pages I read. [UNVERIFIED]
- The CLI adds its own scaffolding on top of the digest, which I did not measure. Set `setting_sources=[]` and a custom system prompt, then read real usage from the result message.
- Equivalent list-price value is about $0.12–0.35 per call [INFERENCE].
- Whether Opus 5.5 is selectable on the owner's plan, and how it counts against limits, is [UNVERIFIED].

## 4. Security handling

**Subscription token.**

- It is a bearer credential with a one-year lifetime and no refresh, scoped to model requests. Source: *Authentication*.
- A revocation procedure for a setup-token is not documented on the pages I read. `/logout` revokes the credential `/login` wrote. [UNVERIFIED] for setup-tokens.
- Store it in a 0600 file, systemd credential or Podman secret, outside the repo and logs.
- Inject it only into the subprocess environment, and make sure `ANTHROPIC_API_KEY` is unset there.
- **Prompt injection is the main risk [INFERENCE].** The digest contains RSS, GDELT and filings text, and the subprocess is an agent that can read files and run commands.
  - Run it with no tools allowed, an empty temporary working directory, `dontAsk`, and no `--allowedTools`.
  - Disable filesystem settings and hooks via `setting_sources`.
  - Exact option names for disabling built-in tools are [UNVERIFIED]. Check the CLI and SDK references.
  - Treat the reply as untrusted: validate against a schema and clamp values.

**API key.**

- Create a dedicated Workspace and key with a spend limit of about $20–30 per month.
- Keep the key in a secret file or Podman secret readable only by the FastAPI process, and rotate it on suspicion.
- The raw API has no tool surface, so injection can only skew the decision text.

## 5. Failure modes and graceful degradation

| Failure | Path | Signal | Handling |
|---|---|---|---|
| Five-hour or weekly limit | Subscription | "Usage limit reached"; error `rate_limit` | No retry. Fall back. |
| Token expired or revoked | Subscription | "Login expired"; `authentication_failed` | Alert owner to re-run `setup-token`. Fall back. |
| Account hold | Subscription | `account_on_hold` | Fall back and alert. Re-login does not clear it. |
| Stray `ANTHROPIC_API_KEY` | Subscription | Unexpected API charges | Unset it. |
| Spend cap hit | API | 429 `enforced_spend_limit_reached`, no `retry-after`; or 400 for a workspace limit | No retry. Fall back and alert. |
| RPM/TPM limit | API | 429 with `retry-after` | Wait that long, one retry. |
| 529 / 5xx / timeout | Both | "API is temporarily at capacity" | The CLI already retries up to 10 times. A 529 does not count against quota. Add a 60–120 s deadline and at most one retry. |
| Bad or missing structured output | Both | `subtype != success` or no `structured_output` | Treat as failure, not a decision. |
| Subprocess hang | Subscription | No result | Hard timeout, kill the process group. |

Sources: *Error reference* (https://code.claude.com/docs/en/errors, 2026); *headless* page (error categories `authentication_failed`, `rate_limit`, `billing_error`, `account_on_hold`, `oauth_org_not_allowed`); *Rate limits*.

**Degradation ladder.**

1. Opus 5.5 call with a hard deadline, one retry, and an idempotency key per (date, decision slot) so nothing is called or billed twice.
2. If it fails and a second credential exists, try it.
3. Otherwise use the local aggregate (qwen3.8-27b plus the debate's `p_outperform_20d`), tagged `source=local_fallback`.
4. If that is unavailable, take the **null action: hold**. The benchmark is buy-and-hold.

Record the source of every decision (`opus`, `local_fallback`, `hold_default`) on the scoreboard so fallback decisions do not contaminate Opus calibration metrics.

## Implications for our setup

**Recommended integration path.**

1. **Primary: API key** with the `anthropic` Python SDK, model `claude-opus-5-5`.
   - One call per decision with a tightly specified JSON output (forced tool call or schema output; parameter name [UNVERIFIED]).
   - Keep effort or thinking low, since thinking is billed as output and is the main cost variable.
   - Use a dedicated Workspace with a $20–30 monthly limit, log `usage` from every response, and wrap the call in the ladder above.
2. **Do not make the subscription path primary.** The saving is about $3–21 per month, which does not justify the terms risk from Consumer Terms §3(7) and §3(9). If the owner chooses it anyway after the checks below, use `claude-agent-sdk` or `claude -p` with the setup-token in a 0600 secret, no tools, `dontAsk`, an empty working directory and `ANTHROPIC_API_KEY` unset, and treat it as a secondary standby.
3. **Keep Opus advisory and single-user.** Use it for the owner's paper-trading research, not as advice to third parties.

**The owner must verify before relying on this.**

1. Whether the Max plan's "$100 to $200/month in API credits" can fund an API key (expiry, rollover, Opus 5.5 eligibility).
2. Whether the current Consumer Terms still contain §3(7) and §3(9) unchanged. Ask Anthropic support whether a single-user `setup-token` script making paper-trading decisions is permitted.
3. The text of the Usage Policy's "High-risk Use Case Requirements" and "Additional Use Case Guidelines (agentic use)", including whether finance is listed.
4. How the Commercial Terms apply to an individual using a personal API key.
5. Real token counts: run the token-counting endpoint on an actual 15–30k digest, measure thinking and output tokens on 10–20 real calls, and recompute the cost table.
6. Which effort or thinking settings and which structured-output parameter the API offers for Opus 5.5.
7. The current usage tier and spend cap in the Console, and that a non-default Workspace spend limit can be set.
8. Subscription path only: the setup-token revocation procedure, the numeric five-hour and weekly allowance on the owner's plan, whether Opus 5.5 is available and how it counts, and the exact options for disabling built-in tools.

**What I could not find.** I found no dated 2025–2026 announcements about enforcement against third-party tools that use subscription credentials. My search tool returned mostly irrelevant results. The policy statements above come from the current docs only, and I claim nothing about past enforcement.
