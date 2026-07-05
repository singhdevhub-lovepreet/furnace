# Raven — An iOS-First Autonomous Development Harness

> A Devin-like agentic engineering platform, purpose-built for iOS/Swift development.
> BYOK (bring your own key), model-agnostic via OpenRouter / Anthropic / OpenAI, with
> real iOS Simulator execution, UI testing, and video-recorded proof of work.

---

## 1. Vision & Positioning

**What it is:** A cloud platform where a user connects a GitHub repo, gives a task in
natural language, and an AI agent autonomously writes Swift code, builds it, runs it in a
**real iOS Simulator**, drives the UI, records video, iterates until tests pass, and opens
a PR.

**Why iOS-specific (the wedge):** General agents (Devin, Cursor, etc.) run on Linux and
**cannot execute iOS apps** — no Xcode, no Simulator, no code signing. iOS devs get code
suggestions but never a closed feedback loop (build → run → see it on a device → fix).
Raven closes that loop. That is the entire moat.

**Core differentiators:**
- Real macOS + Xcode + iOS Simulator execution per session (the hard part nobody offers cheaply).
- Video recording of the agent driving the app (`simctl io recordVideo`) as proof-of-work.
- BYOK + free model choice (OpenRouter, Anthropic, OpenAI, local) — no lock-in, no markup.
- iOS-native context: understands `.xcodeproj`/SPM, XCUITest, SwiftUI/UIKit, provisioning.

---

## 2. The Hard Constraint: macOS Is Mandatory

Everything about the architecture is shaped by one legal/technical fact:

- Xcode, iOS Simulator, `xcrun simctl`, `xcodebuild`, code signing → **Apple hardware only**.
- The macOS EULA **forbids** running macOS on non-Apple VMs. You cannot legally run it on a
  generic EC2 Linux/Windows instance.

**Legitimate macOS compute options (ranked for a startup):**

| Option | Model | Elasticity | Cost profile | Notes |
|---|---|---|---|---|
| **AWS EC2 Mac** (`mac2.metal`, M1/M2) | Dedicated Host | Low (24h min alloc) | ~$0.65–1.08/hr but **24h minimum** per host | Sanctioned "macOS in AWS"; best for pooled always-on fleet |
| **MacStadium (Orka)** | Managed macOS + k8s-like orchestration | Medium | Monthly + on-demand | Purpose-built for CI/agent fleets; VM-level isolation |
| **Scaleway Mac mini** | Bare Mac mini | Medium | Hourly | EU-based, cheaper entry |
| **Cirrus / Codemagic / Bitrise** | CI-as-a-service | High (per-build) | Per-minute | Good for early MVP: no fleet to run |
| **Self-hosted Mac minis** | Colo/office rack | Low | CapEx | Cheapest at scale, ops-heavy |

**Recommendation:** Start MVP on **rented per-build macOS CI (Cirrus/Codemagic)** or a
**single MacStadium host** to avoid the 24h AWS minimum while validating. Move to a
**pooled fleet (EC2 Mac or MacStadium Orka)** once you need warm, sub-second session starts.

> **DECISION (locked, revised):** Spike + MVP compute = **AWS EC2 Mac** (`mac2.metal`),
> funded aggressively via **AWS Activate credits** (up to $100K for provider-backed startups).
> Rationale: Activate credits apply to EC2 Mac dedicated hosts, so early COGS ≈ $0 while we
> prove the loop, and we avoid Orka's ~$499/mo software floor pre-revenue.
> **MacStadium (Orka) is deferred to Phase 2** as the elastic pooled-fleet target once
> utilization justifies it. We must design the macOS runtime provider-agnostically (a thin
> `MacProvisioner` interface) so EC2 Mac → Orka is a swap, not a rewrite.
> Note the EC2 Mac constraint: **dedicated host, 24-hr minimum, billed until the host is
> released even if the instance is stopped** — so keep a small warm host pool and pack
> many sessions onto it rather than allocating a host per request.

**Isolation on macOS:** Unlike Linux you can't just use containers.
- **Ephemeral VMs** via [`tart`](https://github.com/cirruslabs/tart) (open-source, Apple
  Virtualization.framework) — fast clone-per-session macOS VMs on Apple Silicon. **This is
  the key OSS building block** — it's your "Docker for macOS".
- Or Orka (MacStadium) for managed VM orchestration.
- Reset VM from a golden snapshot after each session for clean, sandboxed state.

---

## 3. High-Level Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              CONTROL PLANE                │
                    │            (Linux, cheap, elastic)        │
                    │                                           │
   Browser UI  ───► │  Web app (Next.js) ── API (gateway)       │
   (harness)        │      │                                    │
                    │      ├── Session Orchestrator             │
                    │      ├── Agent Runtime (LLM loop)         │
                    │      ├── Model Router (BYOK)              │
                    │      ├── GitHub App service               │
                    │      ├── Secrets/Vault (per-user keys)    │
                    │      └── Event bus + video/artifact store │
                    └───────────────┬───────────────────────────┘
                                    │ gRPC / WebSocket (tools API)
                    ┌───────────────▼───────────────────────────┐
                    │            macOS WORKER POOL               │
                    │        (Apple hardware, per-session VM)    │
                    │                                            │
                    │  tart/Orka ephemeral macOS VM:             │
                    │   • Xcode + iOS Simulator                  │
                    │   • worker agent (executes tool calls)     │
                    │   • git clone (installation token)         │
                    │   • xcodebuild / simctl / XCUITest         │
                    │   • Appium/WebDriverAgent (UI driving)     │
                    │   • simctl recordVideo → stream to store   │
                    └────────────────────────────────────────────┘
```

**Split rationale:** Keep everything possible on cheap Linux (UI, orchestration, LLM
calls, GitHub, storage). Use expensive macOS **only** for the build/run/test/record inner
loop. macOS worker exposes a thin "tool server" the agent calls remotely.

---

## 4. The iOS Execution Loop (the heart of Raven)

This is what makes Raven ≠ a chat wrapper. The agent's toolbox on the macOS worker:

| Tool | Implementation | Purpose |
|---|---|---|
| `list_simulators` | `xcrun simctl list -j` | discover/boot devices |
| `boot_sim` | `xcrun simctl boot <udid>` | start an iPhone 15 etc. |
| `build` | `xcodebuild -scheme ... -destination` / `swift build` | compile, capture errors |
| `install_app` | `xcrun simctl install booted App.app` | deploy to sim |
| `launch_app` | `xcrun simctl launch booted <bundleid>` | run |
| `ui_snapshot` | XCUITest accessibility tree / Appium `page_source` | "DOM" equivalent for tapping |
| `tap / type / swipe` | Appium (WebDriverAgent) or XCUITest | drive the UI |
| `screenshot` | `xcrun simctl io booted screenshot` | visual verify (feed to VLM) |
| `record_video` | `xcrun simctl io booted recordVideo` | proof-of-work → harness UI |
| `logs` | `xcrun simctl spawn booted log stream` | runtime debugging |
| `run_tests` | `xcodebuild test` (unit + XCUITest) | closed-loop verification |

**No CDP/DOM like web** — iOS UI automation is **accessibility-identifier based**. Two paths:
- **XCUITest** (native, most robust, Swift) — best for authored test suites.
- **Appium + WebDriverAgent** (WebDriver protocol) — lets you drive iOS *and* Android with
  one protocol; better for interactive agent-driven exploration. **Recommend Appium for the
  agent loop, XCUITest for generated test suites.**

**Visual grounding:** feed `simctl` screenshots to a vision model so the agent can "see"
the rendered UI, combined with the accessibility tree for precise element targeting — same
pattern as web computer-use.

---

## 5. BYOK & Model Routing

**Goal:** user brings their own key; picks any model; no markup, no lock-in.

- **Provider abstraction layer** normalizing:
  - **OpenRouter** (single key → hundreds of models; simplest BYOK default)
  - **Anthropic** (Claude Sonnet/Opus direct)
  - **OpenAI** (GPT-4o/o-series direct)
  - **Local/self-hosted** (Ollama, vLLM) for the privacy-conscious
- **Key storage:** per-user encrypted secrets (envelope encryption, e.g. KMS + per-user DEK;
  or self-hostable Vault). Keys are decrypted only in-memory during a session, never logged,
  never sent to the macOS worker (LLM calls originate from the control plane).
- **Model policy per role:** allow different models for planner vs. coder vs. cheap
  summarizer (e.g. Claude Opus to plan, Sonnet to code, Haiku/GPT-4o-mini to summarize).
- **Cost/usage metering** surfaced to the user (tokens, $ spent) since it's their key.
- **Prompt-caching / context management** to keep BYOK bills sane on long sessions.

**Agent framework (DECISION: fork an OSS agent, not custom).** Tool-calling loop: plan →
act (tool) → observe → repeat, with a persistent todo/checklist and self-verification (must
build + pass tests before PR). Rather than write this loop from scratch, Raven **forks a
mature OSS agent** and adds an iOS runtime + iOS toolset.

### 5a. Which OSS agent to fork

| Candidate | What it is | Fit for Raven | Verdict |
|---|---|---|---|
| **OpenHands** (ex-OpenDevin) | Most mature OSS Devin clone: agent abstraction, **event stream** (action/observation), **pluggable Runtime**, web UI, GitHub resolver, **LiteLLM** model layer | Runtime abstraction = clean seam to add a macOS/Orka runtime; LiteLLM already covers OpenRouter/Anthropic/OpenAI/local → **BYOK for free**; UI + sessions already exist | ✅ **Primary fork target** |
| **Goose** (Block) | Extensible agent, **MCP-native** | Great pattern for *exposing* iOS tools (simulator as an MCP server), but thinner as a full hosted harness (no rich session web UI) | ➕ Use its **MCP tool pattern**, not the whole app |
| **SWE-agent** (Princeton) | Agent-computer interface, benchmark-oriented | Strong ACI ideas, but research-shaped, less of a product harness | Reference only |
| **Aider** | git-native pair-programmer CLI | Excellent editing/repo-map logic, but CLI-first, not autonomous multi-tool harness | Borrow repo-map ideas |
| **Devika / gptme / Cline** | Various OSS agents | Less mature runtime abstraction / smaller ecosystems | Skip |

**Plan: fork OpenHands.** Reuse its agent loop, event stream, LiteLLM-based model routing
(satisfies BYOK + model selection with near-zero work), and web UI. Raven's real work =
(1) implement a **macOS/iOS Runtime** that runs on an Orka VM instead of the default Linux
Docker runtime, and (2) register the **iOS tool/action set** (build, boot sim, install,
launch, tap/type via Appium, screenshot, recordVideo, run_tests). Expose the iOS tools as
an **MCP server** (Goose-style) so they're reusable and cleanly decoupled from the agent core.

> Watch-outs when forking: OpenHands' default runtime assumes Linux/Docker — the macOS
> runtime is net-new (the biggest chunk of work). Track upstream to avoid a hard divergence;
> keep iOS additions in clearly separated modules/plugins to ease rebases. License = MIT.

---

## 6. GitHub Integration

- **GitHub App** (not PATs): user installs on selected repos → you get **short-lived
  installation tokens** to clone/push. Cleaner permissions, revocable, per-repo scoping.
- Clone happens **on the macOS worker** using an injected installation token (never persisted
  to the VM image; VM is destroyed after session).
- Agent opens PRs via the App identity; posts build/test/video artifacts as PR comments.
- Webhooks (optional) for event-driven runs (e.g. "fix failing CI", respond to review).

---

## 7. Sandboxing & Security

- **Per-session ephemeral macOS VM** (tart/Orka), destroyed + reset from golden snapshot
  after each run → no cross-tenant leakage.
- Network egress controls on workers (allowlist package registries, GitHub, model providers).
- BYOK keys isolated to control plane; workers never see user LLM keys.
- Code-signing/provisioning secrets (if building for device, not just sim) handled via
  per-user secure storage; **MVP targets Simulator only** → no signing needed, big simplification.
- Audit log of every tool call + artifact for reproducibility.

---

## 8. Recommended Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Web UI | Next.js + React + Tailwind | fast, streaming session view, video playback |
| Realtime | WebSockets / SSE | stream logs, screenshots, video, agent thoughts |
| API / Orchestrator | **Python (FastAPI)** — LOCKED | one language with the OpenHands agent; async + gRPC/WS to workers |
| Worker daemon (in macOS VM) | **Python** (thin) | shells out to xcodebuildmcp/simctl; streams logs/video |
| Agent core | **Fork of OpenHands** (Python) | mature loop + event stream + web UI + LiteLLM (BYOK) |
| Model routing | OpenHands' **LiteLLM** layer | OpenRouter/Anthropic/OpenAI/local out of the box |
| iOS toolset | **adopt `xcodebuildmcp`** (off-the-shelf MCP) + Appium/WDA | don't reinvent; commoditized |
| macOS compute | **EC2 Mac + `tart`** now → **Orka** at Phase 2 (behind `MacProvisioner`) | credits-funded start, swap later |
| iOS automation | Appium + WebDriverAgent (interactive) + XCUITest (suites) | dual protocol |
| Model access | OpenRouter + provider SDKs behind an abstraction | BYOK, model choice |
| Secrets | KMS envelope encryption / HashiCorp Vault | self-hostable for OSS |
| Storage | S3-compatible (MinIO for OSS) | videos, screenshots, artifacts |
| Queue/bus | NATS or Redis Streams | session scheduling, worker dispatch |
| DB | Postgres | sessions, users, repos, usage |

**OSS-friendliness:** every proprietary AWS piece has an OSS swap (MinIO for S3, Vault for
KMS, NATS for SQS, tart for macOS VMs) so self-hosters can run Raven end-to-end — the only
irreducible requirement is **owning/renting Apple hardware**.

---

## 9. Phased Roadmap

**Phase 0 — Spike (prove the loop) [1–2 wks]**
- **Apply for AWS Activate credits** (Founders $1K immediately; pursue Portfolio up to $100K
  via an accelerator/VC Activate Provider) — do this first, in parallel with everything.
- Allocate one **EC2 Mac `mac2.metal`** dedicated host; bake a golden AMI: Xcode + iOS
  Simulator runtimes + `xcodebuildmcp` + Appium/WebDriverAgent.
- Hardcoded repo. Script: clone → `xcodebuild build` → boot sim → install → launch →
  screenshot → `recordVideo`. No agent yet. **Goal: prove closed loop + video capture.**
- Wrap host lifecycle behind a `MacProvisioner` interface (EC2 impl now, Orka impl later).

**Phase 1 — Agentic MVP [3–5 wks]**
- Fork OpenHands; wire BYOK via its LiteLLM layer (OpenRouter first).
- Build the iOS MCP tool server; register it as the agent's toolset.
- Tools: build, run, screenshot, tap/type (Appium), run_tests, logs.
- Basic web UI: submit task, watch streamed logs + screenshots + final video.
- GitHub App: connect repo, clone via token, open PR.
- Single-tenant, single warm macOS VM.

**Phase 2 — Multi-tenant & isolation [4–6 wks]**
- tart/Orka ephemeral VM per session + golden snapshot reset.
- Worker pool + scheduler (queue, warm pool for fast starts).
- Per-user encrypted key vault; model router (Claude/OpenAI/OpenRouter/local).
- Usage metering + cost display.

**Phase 3 — iOS depth & polish [ongoing]**
- XCUITest generation, flaky-test handling, visual diffing.
- SwiftUI preview rendering, multi-device matrix (iPhone/iPad/OS versions).
- Device (not just sim) builds w/ signing (opt-in), TestFlight deploy.
- Android worker reuse (Linux + emulator + Appium) → "mobile" not just iOS.

---

## 10. Cost Realities (be honest upfront)

- macOS compute is the dominant cost. EC2 Mac = 24h-minimum dedicated host; a warm pool of
  N Macs is a real monthly floor. Mitigate with: shared warm pool + fast tart reset,
  aggressive session timeouts, per-build CI for low volume.
- LLM cost is **the user's** (BYOK) — your margin is the platform/orchestration, not tokens.
- Simulator-only MVP avoids Apple Developer Program signing complexity entirely.

---

## 11. Key Risks & Open Questions

- **macOS elasticity:** cold-starting Macs is slow/expensive vs. Linux containers. Warm pool
  is mandatory for good UX → capital/opex tradeoff. → *Decide fleet provider early.*
- **iOS UI automation robustness:** Appium/WebDriverAgent can be flaky; accessibility ids
  must exist. → *Invest in a solid ui_snapshot + retry layer.*
- **Apple ToS drift:** virtualization rules (2-VM-per-host limits, licensing) can change.
- **BYOK support burden:** many providers, changing APIs. → *Lean on OpenRouter as default.*
- **Security of user keys + code:** must be airtight for trust. → *Ephemeral VMs, no key on worker.*

**Questions for you:**
1. **OSS scope** — fully self-hostable (MinIO/Vault/tart) from day one, or hosted-first then open later?
2. **macOS provider** for the spike — MacStadium, EC2 Mac, Scaleway, or per-build CI?
3. **MVP target** — Simulator-only (recommended) or device builds w/ signing too?
4. **Android now or later** — architect for both from the start, or iOS-only MVP?
5. **Agent core** — ✅ *decided:* fork **OpenHands** (add macOS runtime + iOS MCP tools).

**Decisions locked so far:** spike/MVP compute = **AWS EC2 Mac + AWS Activate credits**
(Orka deferred to Phase 2 fleet); agent core = **fork OpenHands**; iOS tools = **adopt
xcodebuildmcp**. Still to confirm: OSS scope (default self-hostable), MVP = Simulator-only
(default), Android later (default).

---

*Next step once you pick directions: I can scaffold the repo — orchestrator + agent loop +
macOS worker tool-server + Appium runner + a Phase-0 script that proves build→run→record on
a real Mac.*

---

## 12. Competitive Landscape (does this already exist?)

Short answer: **the pieces exist, but nobody ships the exact combination** — a *hosted,
multi-tenant, cloud-macOS, GitHub-connected, BYOK autonomous agent* that runs the full iOS
build→simulate→UI-test→record→PR loop for you. What exists clusters into 4 buckets:

**A. Local macOS desktop agents (run on YOUR Mac, BYOK)** — closest in spirit, but not hosted.
- [`hpennington/agentswift`](https://github.com/hpennington/agentswift) — native macOS app; Claude discovers project → implements → builds (xcodebuildmcp) → launches sim → validates. Single-user, local, Anthropic-only.
- [`10x`](https://github.com/10x-app-builder/10x) — macOS app; NL → SwiftUI → XcodeGen scaffold → build → sim preview + screenshot. Local, Claude tool loop.
- [`TomMcGrath7/iOSTestAgents` (MobileTestAI)](https://github.com/TomMcGrath7/iOSTestAgents) — multi-agent, multi-simulator NL testing; supports OpenAI/Claude/**local** LLMs. Testing-focused, local.
> Gap vs Raven: all require the user to own a Mac + set up Xcode; single-tenant; no cloud fleet, no GitHub-App session model.

**B. iOS MCP tool servers (bring your own agent + compute)** — the *tools*, not the harness.
- [`xcodebuildmcp`](https://www.npmjs.com/package/xcodebuildmcp) — build/launch/UI-automation MCP; the de-facto standard (agentswift & MobileTestAI both use it).
- [`kevinswint/xcode-studio-mcp`](https://github.com/kevinswint/xcode-studio-mcp) — unified build/deploy/screenshot/interact MCP (Swift, single binary).
- [`moasq/ios-dev-agent`](https://github.com/moasq/ios-dev-agent) — skills/rules/MCP kit (Apple-account SRP-2FA login, signing, TestFlight, App Store preflight) for 14 AI tools.
> **Big implication for Raven:** the iOS tool layer is now commoditized. **We should NOT build our own iOS MCP from scratch — adopt `xcodebuildmcp`** (and borrow ios-dev-agent's signing/App-Store skills later) and spend our effort on the *hosted cloud-macOS + multi-tenant + GitHub/BYOK harness* nobody offers.

**C. Agentic mobile-testing platforms (QA, not full dev)** — overlap on the run/drive loop.
- [`mobai.run`](https://mobai.run/) — plug Claude Code/Codex/Cursor (or built-in agent) into real phone/sim; taps/scrolls/types iOS+Android; MCP + HTTP API; `.mob` regression scripts.
- [`Autonoma-AI/autonoma`](https://github.com/Autonoma-AI/autonoma) — **OSS** agentic E2E testing (web/iOS/Android), Appium + vision element detection, Temporal, real devices. Good architectural reference for our engine.
- [`callstack/agent-device`](https://github.com/callstack/agent-device) — CLI giving agents token-efficient device snapshots/refs across iOS/Android/RN/Flutter.
> Gap vs Raven: these verify apps, they don't autonomously *develop* features end-to-end and open PRs.

**D. Hosted / self-hosted cloud coding agents (general, not iOS-execution).**
- [`jonatansberg/netclode`](https://github.com/jonatansberg/netclode) — **self-hosted** cloud agent: k3s + Kata microVMs + Tailscale + on-demand GitHub tokens + a native iOS/macOS *client app*. **But the sandboxes are Linux microVMs running Claude Code** — the iOS app is just a phone client, it does NOT run Xcode/iOS Simulator. Great reference for our control-plane + GitHub-token design.
- **[Devin Cloud](https://devin.ai/cloud/)** — model-agnostic parallel cloud agents that launch apps & click through flows with screen recordings… but on **Linux/Windows/Android** — explicitly **no iOS execution**.

### Where Raven is genuinely differentiated (the whitespace)
1. **Hosted cloud-macOS fleet** (Orka) — B & A make *you* bring the Mac; Raven provides it.
2. **Multi-tenant, GitHub-App session model** — connect a repo, get finished PRs (netclode is single-user self-host; desktop agents are local).
3. **Full autonomous dev loop, not just QA** — develop features + record proof + open PR (vs testing-only C).
4. **iOS-native execution that Devin Cloud lacks** — the one thing the big general agent can't do.
5. **BYOK + any model** baked into a hosted product (most hosted agents lock the model; local ones are BYOK but not hosted).

### Revised build vs. buy
- **Buy/adopt:** `xcodebuildmcp` for the iOS tool loop; ideas from `ios-dev-agent` (signing/App Store) and `autonoma` (test engine).
- **Build (the actual moat):** cloud-macOS orchestration on Orka, multi-tenant control plane, GitHub-App flow, BYOK key vault, session UI + video streaming — i.e. the OpenHands fork + Orka runtime, wiring `xcodebuildmcp` as the toolset rather than reinventing it.

> Net: the concept is validated (lots of activity in the last ~6 months) but **fragmented** —
> local agents, tool servers, and QA bots each own one slice. No one has assembled them into
> a hosted "Devin-for-iOS." That assembly + cloud-macOS is the opportunity, and it also means
> less to build than the original plan assumed (reuse the MCP layer).

---

## 13. How Xcode Actually Runs on the macOS VM

**Question: MCP wrapper, or run Xcode "entirely in the box"? → It's both, and they're not opposites.**

- **Xcode IS fully installed and running in the VM.** You cannot avoid this — the Xcode
  install is what ships `xcodebuild`, the Swift toolchain, the iOS **Simulator runtimes**,
  and `simctl`. There is no "Xcode-less" way to build/run an iOS app. So every macOS worker
  VM has a real Xcode (`xcode-select`-ed, license accepted) baked into its golden image.
- **What we do NOT run is the Xcode GUI IDE.** The agent never "uses Xcode.app" like a human
  clicking Run. Instead it drives Xcode's **command-line interface**:
  - `xcodebuild -scheme … -destination 'platform=iOS Simulator,name=iPhone 15'` → compile/test
  - `xcrun simctl boot|install|launch|io … recordVideo|screenshot` → run + capture on the sim
  - `swift build/test` for SPM packages
- **xcodebuildmcp is just the interface layer.** The MCP server is a thin wrapper that shells
  out to exactly those CLI tools and returns structured results. So "MCP vs Xcode" is a false
  choice: **Xcode does the real work; the MCP is how the agent calls it.** The agent emits a
  tool call → MCP runs `xcodebuild`/`simctl` inside the VM → returns build errors, screenshots,
  video paths.
- **The Simulator runs headless.** `simctl` boots the Simulator without a human needing to see
  it; we capture screenshots/video programmatically. (We *can* also expose the live GUI over
  VNC for visual debugging or to stream to the harness UI, but it's optional, not required.)

```
Agent (OpenHands, on Linux) ──tool call──► iOS MCP (xcodebuildmcp, in macOS VM)
                                               │  shells out to
                                               ▼
                                   Xcode toolchain: xcodebuild / xcrun simctl / swift
                                               │
                                               ▼
                                   iOS Simulator (headless) → screenshots + recordVideo
```

**Bottom line:** full Xcode in every VM (mandatory), agent drives it via `xcodebuildmcp`
CLI calls, GUI IDE not used (only optional VNC for visual debugging/streaming).

---

## 14. Mac-VM Pricing & Business Model (BYOK ≠ free compute)

**Your instinct is correct: BYOK covers the LLM, but the cloud Mac is real COGS you must charge for.**
With BYOK, the *user's* API key pays every token — that cost is theirs, not yours. Your cost
(and therefore your pricing) is the **macOS compute + control-plane infra**. A Mac sitting idle
for a user is a pure loss, so the box must be billed.

### What the Mac actually costs (researched)

| Provider | Unit | Price | Gotcha |
|---|---|---|---|
| **MacStadium** individual hosted Mac | M2.S mini (8-core, 8GB) | **~$109/mo** | dedicated, always-on monthly |
| | M4.S mini (10-core) | **~$149/mo** | annual = 10–20% cheaper |
| | M2.L / Pro tiers | ~$199–$349/mo | |
| **MacStadium Orka** (orchestration) | Small Teams Edition | **~$499/mo software floor** + hosts | this is the *elastic ephemeral-VM* layer we planned |
| **AWS EC2 Mac** | `mac2.metal` (M1, 8-core, 16GB) | **~$0.65/hr** ≈ **$468/mo if 24×7** | **dedicated host, 24-hr min, billed while allocated even if instance stopped** — you pay until you *release the host* |

Key cost realities:
- **Orka's ~$499/mo software floor + per-host cost** is heavy for a pre-revenue spike. Its
  value (elastic ephemeral VMs, k8s-style orchestration) only pays off once you run a *pool*.
- **EC2 Mac's 24-hour minimum + host-based billing** kills naive per-request elasticity —
  spinning a fresh host per short session is wildly uneconomical; you must keep warm hosts and
  pack many sessions onto them.
- Either way, **profitability depends on utilization** — multiple users time-sharing warm
  macOS hosts. One idle Mac per user loses money.

### Free credits (researched — these materially de-risk early cost)

- **AWS Activate** — **the real lever.** Founders package = **$1,000** credits (self-funded,
  <10yr, has a website); Portfolio package = **up to $100K** if affiliated with an Activate
  Provider (accelerator/VC/startup org). Credits apply to EC2 **including Mac dedicated hosts**.
  $100K would cover a very large number of Mac-hours → realistically funds the entire MVP + early beta.
- **MacStadium free trial** — historically a **1-month free** Mac mini trial (promo codes).
  Good for the Phase-0 spike at zero cost.
- **MacStadium FOSS Open Source Program** — a **free Mac mini**, but **only for non-commercial
  FOSS** (unpaid lead, no commercial funding/sponsorship). If Raven's core stays genuinely
  non-commercial OSS it *might* qualify, but don't build the business plan on it.

### Recommended pricing model for Raven
- **Charge for Mac session time** (the metered COGS): per-minute/hour of active macOS worker
  time, passed through with margin — cleanest match to how the Mac is billed.
- Or **subscription tiers** with included session-hours + overage (better revenue predictability).
- **LLM = BYOK, zero markup** (your differentiator) — surface the user's own token spend so
  it's transparent, but you don't touch it.
- Margin comes from **packing utilization** on warm hosts, not from tokens.

### Reconciling with the locked "MacStadium (Orka)" choice
- **Phase 0/1 (spike + MVP):** start on a **single always-on Mac** — a MacStadium hosted mini
  (use the free-trial month; ~$109–149/mo after) *or* an **EC2 Mac host funded by AWS Activate
  credits**. **Skip Orka's $499/mo floor until you need a pool.** Fastest, cheapest path to
  prove build→run→record.
- **Phase 2 (multi-tenant fleet):** adopt **Orka** for elastic ephemeral macOS VMs + snapshot
  reset — its cost model makes sense once utilization justifies it.
- So Orka remains the *scale-out* target, but it is **not** the cheapest way to run
  the first spike — flag for decision.

---

## 15. EC2 Mac — Golden AMI Recipe & Warm-Pool / Host-Packing Design

### 15.1 The billing model that dictates the whole design

- **Unit of billing = the Dedicated Host, not the instance.** You allocate a host; exactly
  one `mac2.metal` instance runs on it. You pay **from host allocation until host release**,
  even if the instance is stopped.
- **24-hour minimum allocation** (Apple EULA). You cannot release a host before 24h.
- **~50–60 min first-boot / stop-start** for Mac instances (EBS-backed, scrubbing between
  instance stops). This is slow — you do **not** want to allocate/release hosts per user session.

**Design consequence:** treat EC2 Mac hosts as a **long-lived warm pool** (allocated for
days/weeks), and multiplex **many short user sessions** onto each host via lightweight,
fast-reset **nested VMs**. Never map "1 user session = 1 host".

### 15.2 Two-level isolation: host → nested ephemeral VM

Because a host holds one macOS instance, per-session isolation comes from **nested
virtualization inside** that instance:

```
EC2 Dedicated Host (mac2.metal, allocated for weeks)
└── macOS instance (the "base" — AMI-baked with Xcode + tooling)
    └── tart VM #1  (ephemeral, one user session)   ← cloned from golden local image
    └── tart VM #2  (ephemeral, one user session)
    └── ...           (as many as RAM/CPU allow — see packing)
```

- **[`tart`](https://github.com/cirruslabs/tart)** (Apple Virtualization.framework) runs
  nested macOS VMs *inside* the EC2 Mac instance, cloned from a local golden image in seconds
  and destroyed after each session → clean, sandboxed per-tenant state.
- This is exactly the primitive Orka manages for us later; using `tart` now keeps the
  `MacProvisioner` seam honest (EC2+tart impl now, Orka impl later — same "give me a fresh
  ephemeral macOS VM" contract).
- Apple's virtualization framework historically caps at **2 nested VMs per host** — verify
  current limits; this directly bounds sessions-per-host and thus unit economics.

### 15.3 Golden image contents (baked once, reused everywhere)

Two artifacts to bake: (a) the **EC2 AMI** for the base instance, (b) the **tart golden VM
image** that sessions clone from. Both need the same iOS stack:

| Layer | What | Notes |
|---|---|---|
| OS | macOS (matching Xcode requirement) | license/EULA accepted |
| Xcode | Full Xcode via `xcodes` CLI (`xcodes install <ver>`) | pin version; `sudo xcodebuild -license accept` |
| CLI toolchain | `xcode-select -s`, `xcodebuild -runFirstLaunch` | accept, install additional components |
| Simulator runtimes | pre-download needed iOS runtimes (`xcodebuild -downloadPlatform iOS`) | avoids slow first-run download per session |
| iOS tool MCP | **`xcodebuildmcp`** (`npm i -g xcodebuildmcp`) | the agent's iOS tool interface |
| UI automation | **Appium + WebDriverAgent** (or XCUITest runner) | pre-build WDA to avoid per-session signing/build |
| Runtimes | Node.js (for MCP/Appium), Homebrew | |
| Worker agent | Raven worker daemon (executes tool calls, streams logs/video) | connects back to control plane over gRPC/WS |
| Warm-up | pre-boot a Simulator + do one throwaway `xcodebuild` | primes caches so first real build is fast |

**Build automation:** use **Packer** (has an EC2 Mac builder) to produce the AMI reproducibly,
and a scripted `tart` image build for the nested VM. Version both; rebuild on Xcode bumps.

### 15.4 Session lifecycle (fast path)

```
user starts session
  → scheduler picks a warm host with free capacity
  → `tart clone golden session-<id>` (seconds)
  → `tart run` + inject short-lived GitHub token, clone repo
  → worker daemon attaches; agent drives xcodebuildmcp/simctl/Appium
  → recordVideo streams artifacts to control-plane store
session ends
  → `tart delete session-<id>`  (full teardown, no residue)
  → host stays warm for the next session
```

- **No host allocation on the hot path** → session start is *seconds* (tart clone), not the
  ~50-min instance cold boot.
- Snapshot reset per session = strong tenant isolation without re-baking.

### 15.5 Warm-pool sizing & host-packing (the unit economics)

- **Capacity per host** = min(nested-VM cap, RAM/CPU headroom). `mac2.metal` = M1/8-core/16GB
  → memory is the binding constraint; a Simulator + Xcode build is RAM-hungry, so realistically
  **1–2 concurrent sessions per host** (confirm empirically). This number *is* the business model.
- **Pool controller** (control-plane service):
  - Keeps `N` hosts allocated based on rolling demand; **respects the 24-h min** before releasing.
  - **Scale-up:** allocate a new host when free capacity < threshold (but new host = ~50 min to
    warm → keep a buffer; consider a small standby host).
  - **Scale-down:** only release hosts idle **and** past 24-h min; drain sessions first.
  - **Bin-packing:** place new sessions on the fullest host that still fits (maximize packing,
    minimize host count) rather than spreading.
- **Queueing:** if all hosts full and warming a new one takes ~50 min, queue the session with an
  honest ETA rather than allocating recklessly. Premium tier could reserve guaranteed capacity.
- **Idle reaping:** hard session timeouts + idle detection so one user can't pin a slot.

---

## 16. `MacProvisioner` Interface Spec (EC2+tart now, Orka later)

**Goal:** the entire control plane talks to macOS capacity through **one narrow interface**
so swapping EC2+tart → Orka (or adding self-hosted Mac minis) is a driver change, never a
rewrite. The provider's *only* job: hand back a ready-to-use, isolated **macOS session VM**
and tear it down. Everything above it (scheduling, agent, GitHub, billing) is
provider-agnostic.

### 16.1 Design principles
- **Session-centric, not host-centric.** Callers ask for a *session VM*; the provider hides
  hosts, nesting, pools, and the 24-h-min accounting.
- **Handle-based.** Every op takes/returns an opaque `SessionHandle` — callers never learn
  provider internals (host IDs, tart names, Orka node refs).
- **Idempotent + cancelable.** `acquire`/`release` must be safe to retry; a crashed control
  plane must be able to reconcile (see `list`).
- **Capabilities, not assumptions.** Providers advertise what they support (nested-VM cap,
  live GUI stream, Apple-silicon gen) so the scheduler adapts instead of hard-coding EC2 facts.

### 16.2 Core interface (language-neutral)

```
interface MacProvisioner:
    # Lifecycle of a single isolated macOS session VM
    acquire(spec: SessionSpec) -> SessionHandle        # blocks until READY or QUEUED w/ ETA
    release(h: SessionHandle) -> void                  # destroy VM; free capacity (NOT the host)
    status(h: SessionHandle) -> SessionStatus          # PROVISIONING|QUEUED|READY|DRAINING|GONE
    exec(h, cmd, argv, env, timeout) -> ExecResult     # run in the session VM (stdout/stderr/code)
    open_channel(h, kind) -> Stream                    # kind: LOGS | VNC | ARTIFACTS | TOOL_RPC
    put_file(h, path, bytes) / get_file(h, path)       # inject GitHub token / pull artifacts

    # Fleet-level (used by the pool controller, not the agent)
    capabilities() -> ProviderCaps                     # static: caps, gen, max nested, features
    capacity() -> CapacityReport                       # live: free/used session slots, warm hosts
    reconcile() -> list[SessionHandle]                 # recover state after control-plane restart

SessionSpec:
    image_id: str            # golden image/version (Xcode ver pinned)
    cpu, memory_hint         # scheduler hint; provider maps to a VM profile
    ttl / idle_timeout       # hard caps; provider self-reaps
    warm: bool               # prefer an already-warm slot (fast path)
    features_required: set   # e.g. {VNC_STREAM}

SessionHandle: { id, provider, created_at, tool_endpoint }   # opaque, serializable
ProviderCaps: { max_sessions_per_host, nested_vm_cap, silicon_gen, supports_vnc, min_alloc_seconds }
CapacityReport: { free_slots, total_slots, warm_hosts, provisioning_hosts, est_new_slot_seconds }
```

> Note: `acquire` returning **QUEUED + ETA** (not just READY) is deliberate — it lets the EC2
> driver surface the ~50-min warm-host reality honestly instead of blocking opaquely.

### 16.3 Driver A — `Ec2TartProvisioner` (now)

Maps the interface onto EC2 Mac + nested `tart`:
- `acquire`: scheduler picks a warm host with a free slot → `tart clone <image_id> <sid>` →
  `tart run` → boot worker daemon → return handle with `tool_endpoint`. If no slot: either
  trigger a host allocation (background, ~50 min) and return **QUEUED+ETA**, or queue on
  existing hosts.
- `release`: `tart delete <sid>` (frees a slot); **host stays allocated** (warm pool owns
  host lifecycle, honoring 24-h min).
- `exec`/`open_channel`: over the worker daemon (gRPC/WS) inside the tart VM.
- `capabilities`: `{ nested_vm_cap: 2, silicon_gen: M1, min_alloc_seconds: 86400, supports_vnc: true }`.
- **Host accounting lives in the pool controller**, but the *driver* exposes `capacity()` and
  a separate internal host-manager (`allocateHost`/`releaseHost`) the controller calls — kept
  out of the session interface so Orka doesn't inherit EC2's host concept.

### 16.4 Driver B — `OrkaProvisioner` (Phase 2)

- `acquire`: Orka API "deploy VM from image" → returns node/IP → attach worker daemon.
- `release`: Orka "delete VM".
- Orka manages hosts/nodes itself, so the host-manager internals are a **no-op** here — this
  is exactly why host accounting is *outside* the session interface.
- `capabilities`: Orka's per-node VM density, likely higher than EC2's 2-cap → scheduler packs more.

### 16.5 Driver C — `LocalMacProvisioner` (dev/self-host/OSS)

- Wraps `tart` on a single Mac (or the dev's laptop) with the same contract → lets
  contributors run Raven end-to-end without cloud, and powers CI of the harness itself.

### 16.6 What stays provider-agnostic above the seam
- **Scheduler / pool controller** consumes `capacity()` + `capabilities()` and calls
  `acquire/release`; its bin-packing is generic (only the density numbers differ per provider).
- **Agent runtime (OpenHands fork)** only ever uses `exec` + `open_channel(TOOL_RPC)` to reach
  `xcodebuildmcp` — it has no idea whether it's on EC2 or Orka.
- **GitHub token injection** = `put_file`; **artifact/video pull** = `get_channel(ARTIFACTS)` —
  identical across providers.

**Net:** picking EC2+tart now costs us nothing in lock-in — Orka later is a new class
implementing `MacProvisioner`, plus a config flag. The 24-h-min/host mess is quarantined in
one driver's internal host-manager.

### 15.6 Cost math (sanity check, on-demand list price)

- `mac2.metal` ≈ **$0.65/hr** → **~$15.6/host/day**, **~$468/host/mo** (24×7).
- At **2 sessions/host** and decent utilization, per-active-session-hour COGS ≈ **$0.33–0.65**
  → charge (e.g.) **$1–3 / active hour** or bundle into tiers for healthy margin.
- **AWS Activate credits** ($1K → $100K) cover this entirely through MVP/early beta, so
  real cash COGS ≈ $0 while validating; the model only needs to close **after** credits.
- Savings Plans give **up to 44% off** with a 1–3yr commit — revisit once demand is steady.

### 15.7 Risks specific to this path

- **Nested-VM cap (≤2) throttles density** → watch unit economics; Orka/bare metal may pack better later.
- **~50-min host warm time** makes burst demand hard → warm buffer + queueing required.
- **EC2 Mac regional availability / quotas** can be limited → request quota early, pick region deliberately.
- **AMI drift on Xcode releases** → automate Packer rebuilds; keep last-known-good.
- **Activate credit expiry** (typically ~1–2 yrs) → have the paid model ready before they lapse.
