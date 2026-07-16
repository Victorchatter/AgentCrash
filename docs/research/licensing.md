# AgentCrash Licensing Research

Date: 2026-07-15
Status: Research — recommendation at the end

## 1. The decision in one paragraph

AgentCrash should ship as **two distinct artifacts with two distinct licenses**:

1. **Core SDK + CLI** (instrumentation, record/replay, crash capture, local analyzer, test generator): **Apache-2.0**. Permissive, OSI-approved, patent grant, maximum adoption, no friction for commercial users to embed in their stack. This is the Playwright/Microsoft playbook.
2. **Managed cloud / hosted service code** (multi-tenant control plane, hosted dashboard, billing, cloud-only collaboration features): **Business Source License 1.1 (BUSL/BSL 1.1)** with a 4-year change date reverting to Apache-2.0, and an Additional Use Grant that permits all production use except offering AgentCrash as a competing hosted/managed service. This is the Sentry/Couchbase/HashiCorp playbook, tuned to avoid their mistakes.

This "permissive core + source-available cloud" split is the dominant 2020s pattern for developer-tools companies that want both wide adoption and protection from closed-source SaaS reselling without contribution. It is exactly what Meilisearch converged on (MIT core + BUSL EE) and what Sentry has run since 2019.

---

## 2. License comparison

| License | OSI-approved | Copyleft | Cloud-SaaS protection | Adoption friction | Patent grant | Notes |
|---|---|---|---|---|---|---|
| **MIT** | Yes | None | None | Lowest | No | Maximum adoption; no defense at all. Good for SDKs you want embedded everywhere. |
| **Apache-2.0** | Yes | None (permissive) | None | Very low | **Yes** | MIT + explicit patent grant + NOTICE/attribution. The "safe permissive" default for serious tools. |
| **MPL-2.0** | Yes | Weak, **file-level** | None (can be combined into proprietary apps) | Low | Yes | Compels sharing of modifications to *existing* MPL files only; new files can stay proprietary. Good middle ground for libraries you want modifications back on. |
| **AGPL-3.0** | Yes | Strong, **network** (Section 13) | Indirect: modified versions served over a network must offer source to all users | High — many companies have a "no AGPL deps" policy | Yes | OSI-approved but widely banned as a dependency. Closes the ASP loophole but also scares off legitimate enterprise users. |
| **SSPL** | **No** | Very strong | Explicit: offering-as-a-service requires open-sourcing your *entire* infra/management stack | Very high | Yes | MongoDB's license. Too aggressive; rejected by OSI. Effectively a cloud-provider veto. |
| **Elastic License 2.0 (ELv2)** | No | None | Explicit: cannot offer the software to third parties as a hosted/managed service | Low-medium | No | Short, readable, narrowly targeted at the cloud-reseller threat. No copyleft. Elastic's default distribution license. |
| **BUSL / BSL 1.1** | No | None (until change date) | Configurable via "Additional Use Grant"; typically blocks competing hosted service | Medium (source-available, not OSI) | No (unless change license grants) | Time-delayed open source: converts to Apache-2.0 (or chosen license) after a stated change date. Sentry/CockroachDB/Couchbase/HashiCorp/Meilisearch EE. |
| **PolyForm Shield** | No | None | Noncompete with the licensor's products | Medium | No | Plain-language BSL-like license; no built-in change date (use PolyForm Countdown for that). |
| **PolyForm Noncommercial** | No | None | Bans all commercial use | High | No | Too broad for a dev tool that legit enterprises must run internally. |

### Key distinctions people conflate

- **"Source-available" vs "open source":** Only OSI-approved licenses (MIT, Apache-2.0, MPL-2.0, AGPL-3.0) can legally be called open source. BUSL, ELv2, SSPL, PolyForm are *source-available* — code is public but usage is restricted. Calling BUSL code "open source" is a common PR mistake that erodes trust.
- **ELv2 vs SSPL vs AGPL:** ELv2 = "you can't host this as a managed service" (no copyleft, no source-sharing duty). SSPL = "if you host this as a service, open-source your whole stack" (cloud-veto, rejected by OSI). AGPL = "if you *modify* and serve over network, share your modifications" (OSI-approved, but scares enterprises).
- **BSL vs PolyForm Shield:** BSL has a **change date** that auto-converts to a true OSS license (eventual open source). PolyForm Shield is restrictive forever unless you use PolyForm Countdown. BSL's time-delay is what makes it politically defensible to the community.

---

## 3. What each company chose and why

### Sentry — BSL 1.1 (Nov 2019)
- **From:** BSD-3-Clause. **To:** BSL 1.1, change date 36 months → Apache-2.0.
- **Why:** VC-funded startups were cloning Sentry's code, docs, and marketing under "it's BSD, we can." AWS was seen as an existential threat. Sentry explicitly rejected the "open-core / free-bad-version + expensive-good-version" model — they wanted no feature gap between self-host and cloud.
- **Scope:** Only the Sentry server repo. **SDKs stayed BSD/Apache** (permissive, so everyone instruments with them). This is the canonical "permissive SDK + restricted server" split.
- **Controversy:** BSL is not OSI-approved; some users felt betrayed. The 36-month conversion was the concession.
- Sources: [Sentry blog](https://blog.sentry.io/relicensing-sentry/), [Business Insider](https://www.businessinsider.com/sentry-david-cramer-bsl-amazon-open-source-2019-11)

### Playwright — Apache-2.0 (ongoing)
- **License:** Apache-2.0, Microsoft + Google copyright. Requires a **Microsoft CLA** for contributions.
- **Why:** Playwright is an adoption play — Microsoft wants every dev team using it, including inside competing clouds. Apache-2.0 (not MIT) was chosen for the **explicit patent grant** and NOTICE/attribution discipline. No cloud-protection needed because Microsoft's commercial moat is Azure + tooling, not a hosted Playwright service.
- **Lesson for AgentCrash:** If the core *is* the strategic asset you want everywhere, Apache-2.0 is the ceiling of permissiveness worth using. MIT is fine but Apache-2.0's patent grant matters for a tool that hooks into other vendors' runtimes.
- Sources: [microsoft/playwright LICENSE](https://github.com/microsoft/Playwright/blob/main/LICENSE)

### Meilisearch — MIT (Community Edition) + BUSL 1.1 (Enterprise Edition, 2025)
- **From:** MIT (whole project). **To:** dual model — CE stays MIT; EE (sharding, analytics, fine-grained access control) is BUSL 1.1, change date 4 years → MIT.
- **Why:** Sustainable growth; enterprise features only large deployments need go under BUSL. Free EE licenses available for indie/non-profits on request.
- **Lesson:** You can keep the developer-facing core fully permissive and gate only the *enterprise-grade* surface behind BUSL. This maps cleanly onto AgentCrash: local crash tool = MIT/Apache core; multi-tenant cloud scale + RBAC + billing = BUSL.
- Sources: [Meilisearch Enterprise Edition](https://www.meilisearch.com/blog/enterprise-license)

### Couchbase — BSL 1.1 (Server 7, 2021)
- **From:** Apache-2.0. **To:** BSL 1.1, change date 4 years → Apache-2.0.
- **Why:** Server 7 had features attractive enough to fork into commercial derivatives. Additional Use Grant permits production use as long as you're not building a commercial derivative / DBaaS. Notably, Couchbase said it was **not under cloud-provider threat** at the time — this was a general anti-forking measure.
- **Lesson:** BSL isn't only for the AWS threat; it's a general "no free-riding commercial fork" tool. The 4-year revert to Apache was positioned as "more permissive over time" vs AGPL/SSPL which restrict forever.
- Sources: [Couchbase BSL blog](https://www.couchbase.com/blog/couchbase-adopts-bsl-license/)

### HashiCorp — BSL 1.1 (Terraform et al., Aug 2023)
- **From:** MPL-2.0. **To:** BSL 1.1, change date 4 years → MPL-2.0. APIs/SDKs stayed MPL-2.0.
- **Why:** Vendors were monetizing HashiCorp's community-built projects without contributing back. BSL blocks "competitive offerings" to HashiCorp products/services.
- **The blowback (cautionary tale):** This triggered the **OpenTofu** fork (Linux Foundation, 140+ orgs, drop-in replacement) and a widespread "bait-and-switch" backlash. The single most cited enabler of the relicense was the **CLA** every contributor had signed, which granted HashiCorp relicensing rights — users felt the trust they'd contributed under had been abused.
- **Lessons for AgentCrash:**
  1. If you ship infrastructure-style code under a permissive/MPL license and build a dependent community, **a later switch to BSL will fork your project.** Decide the split *now*, at launch, not after adoption.
  2. A CLA that grants relicensing rights is a **political liability** as much as a legal asset. If you want the right to relicense later, be honest about it up front, or accept that you can't.
  3. "Competitive offering" in BSL must be defined crisply — HashiCorp's vagueness created real uncertainty for users building on Terraform.
- Sources: [HashiCorp announcement](https://www.globenewswire.com/news-release/2023/08/10/2723189/0/en/HashiCorp-adopts-the-Business-Source-License-for-future-releases-of-its-products.html), [OpenTofu / Linux Foundation](https://www.linuxfoundation.org/press/announcing-opentofu), [LWN analysis](https://lwn.net/Articles/942346/)

### Elasticsearch — Apache-2.0 → SSPL+ELv2 (2021) → +AGPL-3.0 triple (2024)
- **Why:** AWS launched a competing managed Elasticsearch service without contributing. Elastic added SSPL (cloud-veto) + ELv2 (no-hosted-service). In 2024 they **added AGPL-3.0 to restore the "open source" label** — Elastic explicitly said the SSPL/ELv2 move cost them community trust and the AGPL addition was a course-correction.
- **Lesson:** The triple-license is a symptom of having picked politically-toxic licenses. AgentCrash should avoid needing a triple license by choosing well *once*. AGPL-3.0 is the only OSI-approved option that closes the network loophole, but it is widely banned as a dependency — fine for the *server* of a hosted product, dangerous for a *library* others must embed.
- Sources: [Elastic: open source again](https://www.elastic.co/blog/elasticsearch-is-open-source-again), [ELv2 FAQ](https://www.elastic.co/licensing/elastic-license/faq)

---

## 4. Implications for AgentCrash's architecture

### 4.1 Permissive core SDK vs managed/cloud feature — the split

The recurring winning pattern, and the one AgentCrash should follow:

| Artifact | License | Why |
|---|---|---|
| `agentcrash-sdk` (instrumentation, record/replay, crash capture) | **Apache-2.0** | You want every dev team to embed this in their test suite and prod app with zero legal review. Apache-2.0's patent grant removes a common objection. |
| `agentcrash-cli` (local replay, analyze, test-gen) | **Apache-2.0** | Local-only tool; no cloud moat to protect. Max adoption. |
| `agentcrash-server` (multi-tenant control plane, hosted dashboard, billing, cloud-only collab) | **BUSL 1.1**, change date 4y → Apache-2.0, Additional Use Grant = "production use allowed except offering AgentCrash as a hosted/managed service" | This is the cloud moat. BUSL is more defensible and more community-accepted than ELv2 because of the time-delayed conversion. |
| Optional: enterprise self-host features (SSO, RBAC, audit, scale) | **BUSL 1.1** (same as server) | Mirror Meilisearch EE. |

**Critical design rule:** the Apache-2.0 core must be **fully functional standalone** — record, replay, analyze, generate tests locally — so the permissive license is honest and not a crippled "open-core" demo. Sentry's "no feature gap" principle. The cloud features are about *multi-tenant hosting, collaboration, and scale*, not about withholding core capability. If you gate the actual crash analysis behind BUSL, the Apache-2.0 core is a lie and you'll get the HashiCorp-style backlash.

### 4.2 Accepting contributions

- **Do NOT require a broad CLA that grants relicensing rights over the Apache-2.0 core.** That is the exact mechanism that let HashiCorp relicense Terraform and sparked OpenTofu. For the Apache core, use the **DCO (Developer Certificate of Origin)** with `Signed-off-by` commit trailers — "inbound == outbound," no relicensing rights granted, lightweight, trusted by Linux/LF communities. This structurally protects the core from a future bait-and-switch and signals trustworthiness.
- **For the BUSL server component**, contributions are expected to be rare (mostly your team). If you do accept external PRs there, a **narrow CLA** granting only the right to distribute under BUSL + the change-date license (Apache-2.0) is appropriate and should be explicit that it does **not** grant rights to relicense to anything else. Be honest in CONTRIBUTING about the dual structure.
- **CLA vs DCO decision matrix:**

| Goal | Use |
|---|---|
| Max community trust, no future relicensing of core | **DCO** |
| Want right to relicense later / dual-license commercially | **CLA** (but accept political risk) |
| Corporate contributors whose employers need to sign off | **CCLA + individual DCO** (hybrid) |

For AgentCrash: **DCO for the Apache-2.0 core; narrow CLA only for the BUSL server.** Publish the CLA text in the repo.

### 4.3 Dependency license compatibility

AgentCrash's expected stack and the compatibility verdict:

| Dependency | Its license | Compatible with Apache-2.0 core? | Compatible with BUSL server? |
|---|---|---|---|
| FastAPI | MIT | Yes | Yes (BUSL is permissive w/ use restriction, not copyleft) |
| React | MIT | Yes | Yes |
| SQLite | Public domain | Yes | Yes |
| Pydantic | MIT | Yes | Yes |
| uv / Ruff (Astral) | MIT/Apache | Yes | Yes |
| Playwright (if used for replay) | Apache-2.0 | Yes | Yes |
| OpenTelemetry SDKs | Apache-2.0 | Yes | Yes |

**Hard rules to bake into CONTRIBUTING / a `cargo deny`/`pip-licenses` CI step:**

1. **No AGPL-3.0 dependencies in the Apache-2.0 core.** AGPL's network copyleft (Section 13) would force the combined work to be AGPL when served over a network, and AGPL's strong copyleft propagates into the derivative — destroying the permissive core. Many enterprises also have a blanket "no AGPL deps" policy, so an AGPL transitive dep would block adoption. (Compatibility is technically one-way: MIT/Apache *can* be pulled into an AGPL work, but not the reverse.)
2. **No SSPL dependencies anywhere.** Same enterprise-ban problem, worse.
3. **No BUSL/ELv2/PolyForm-Noncommercial dependencies in the core.** These are source-available, not OSI-approved, and will trip corporate license scanners. The core must be composed only of OSI-approved deps.
4. **GPL/LGPL:** LGPL-3.0 is tolerable for *dynamic-linked libraries* in a permissive product (weak copyleft on the lib only), but GPL-3.0 is not (strong copyleft propagates). Prefer MIT/Apache alternatives. Add a CI check.
5. **MPL-2.0 deps are fine** in the Apache core — file-level copyleft, compatible both directions, just remember to keep MPL files identifiable and disclose them on distribution.
6. **The BUSL server** may legally depend on anything compatible, but for *trust* reasons keep the same no-AGPL/no-SSPL rule so users who self-host the server aren't surprised.

### 4.4 Trademark

License ≠ trademark. Apache-2.0 and BUSL do not grant trademark rights. File a **trademark policy** reserving the "AgentCrash" name and logo so a reseller can't market a hosted clone as "AgentCrash." This is cheap, independent of the copyright license, and is often the actual thing that stops cloud resellers (cf. Elastic's trademark enforcement against AWS).

---

## 5. Required community / governance files

These are table-stakes for a serious OSS dev tool and are checked by license-scanners and corporate approval processes (notably the TODO Group / OSS lifecycle, and GitHub's community standards).

| File | Purpose | Notes |
|---|---|---|
| `LICENSE` | The copyright license | One per repo. Apache-2.0 in core repos; BUSL 1.1 + change-date notice in server repo. |
| `LICENSES/` or `NOTICE` | Third-party license inventory | Required by Apache-2.0 §4; list all bundled deps + their licenses. Automate with `pip-licenses`/`cargo about`. |
| `CONTRIBUTING.md` | How to contribute, DCO sign-off requirement, CLA pointer for server repo | State the dual-license structure explicitly. Require `Signed-off-by` for core. |
| `CODE_OF_CONDUCT.md` | Contributor behavior | Use the **Contributor Covenant 2.1** (de facto standard). |
| `SECURITY.md` | Vulnerability reporting policy | Private disclosure channel (security@… or a private vuln reporting form), SLA for response, supported versions table. Required by GitHub Security Advisories and most corporate approvals. |
| `GOVERNANCE.md` | Decision-making, maintainer roles, how one becomes a maintainer | For a single-founder project this can be short ("AgentCrash is currently maintained by Victor; we intend to add a steering committee when N maintainers…"). Its presence signals maturity even when small. |
| `DCO.txt` or reference to developercertificate.org | The DCO text | Reference is fine; the `Signed-off-by` convention binds it. |
| `CLA.md` (server repo only) | The narrow CLA for BUSL contributions | Include the actual agreement text or a link to a signed CLA tool (CLA Assistant). |
| `TRADEMARK.md` | Name/logo usage policy | Reserve "AgentCrash"; permit honest uses (e.g., "powered by AgentCrash"), forbid rebranding a hosted clone. |
| `CHANGELOG.md` | Release history | Not legally required but expected for dev tools. |
| `README.md` | Must state the license, the dual structure, and link to all of the above. | First thing a corporate lawyer reads. |

### Recommended README license section (copy)

> AgentCrash is distributed under a dual-license model. The **core SDK and CLI** (`agentcrash-sdk`, `agentcrash-cli`) are licensed under the **Apache License 2.0**. The **AgentCrash server** (multi-tenant control plane and hosted features) is licensed under the **Business Source License 1.1**, which permits all production use except offering AgentCrash as a hosted/managed service, and automatically converts to Apache-2.0 four years after each release. See `LICENSE` in each repository. Contributions to the core are accepted under the DCO; contributions to the server require the CLA in `CLA.md`.

---

## 6. Recommendation for AgentCrash

1. **Core SDK + CLI: Apache-2.0.** Not MIT — the patent grant matters for a tool that instruments other vendors' runtimes, and Apache-2.0 is the license corporate legal teams approve fastest.
2. **Server / managed cloud: BUSL 1.1, change date 4 years → Apache-2.0, Additional Use Grant permitting all production use except offering AgentCrash as a hosted/managed service.** Define "hosted/managed service" precisely (avoid HashiCorp's vague "competitive offering").
3. **Decide the split at launch.** Do not ship Apache-2.0 today and "relicense later" — that is the HashiCorp/OpenTofu mistake. The core must be genuinely useful standalone.
4. **DCO for core contributions (no broad CLA). Narrow CLA only for the BUSL server.** Publish both. This preserves community trust and avoids the bait-and-switch liability.
5. **Trademark policy** reserving "AgentCrash" name/logo — independent of and in addition to the copyright license.
6. **CI license audit** (`pip-licenses` / `cargo deny` / `license-checker`) blocking AGPL, SSPL, BUSL, ELv2, PolyForm-Noncommercial, GPL-3.0 from the Apache-2.0 core. Keep the core's dependency tree purely OSI-approved.
7. **Ship the community files on day one:** `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md`, `GOVERNANCE.md`, `TRADEMARK.md`, `CHANGELOG.md`, and `CLA.md` in the server repo. Their presence is itself a signal of seriousness that accelerates enterprise adoption.
8. **Call the BUSL component "source-available," never "open source."** The one PR mistake to never make.

### Why not the alternatives

- **AGPL-3.0 for everything:** OSI-approved and protects the network loophole, but widely banned as a dependency — would gut SDK adoption. Consider AGPL only if you ever want a single-license server that's OSI-approved; even then, BUSL is friendlier to self-hosting enterprises.
- **ELv2 for the server:** Cleaner and shorter than BUSL, but no change-date conversion → no "eventually open source" story, which is what makes BUSL politically survivable. Prefer BUSL.
- **MPL-2.0 for the core:** A reasonable choice (file-level copyleft gets modifications back), but it adds a disclosure obligation Apache-2.0 doesn't, and for an *SDK* the goal is zero-friction embedding — Apache-2.0 is the better fit. MPL-2.0 would be the right pick if the core were a *library* where you specifically want modifications contributed back.
- **PolyForm Shield:** Fine alternative to BUSL but lacks the change-date conversion that defuses community anger. Use BSL instead.
- **Pure MIT/Apache for everything:** Maximizes adoption but provides zero protection against a cloud reseller repackaging your server as a hosted product with no contribution — the exact problem Sentry/Elastic/MongoDB/HashiCorp hit. Not appropriate for the server component.

---

## 7. Summary table: the recommendation

| Component | License | Contributions | Change date |
|---|---|---|---|
| `agentcrash-sdk` | Apache-2.0 | DCO (`Signed-off-by`) | n/a |
| `agentcrash-cli` | Apache-2.0 | DCO | n/a |
| `agentcrash-server` | BUSL 1.1 | narrow CLA (BUSL → Apache only) | 4y → Apache-2.0 |
| Enterprise self-host features | BUSL 1.1 (with server) | narrow CLA | 4y → Apache-2.0 |
| Name & logo | Trademark reserved | n/a | n/a |