# Dutch taxation of private investments (box 3), 2025–2028

Research date 2026-10-21. **[LAW]** = in force. **[PROPOSAL]** = not law. **[UNVERIFIED]** = not confirmed from a primary source. **[INFERENCE]** = my deduction.

Sources:
- **WetIB** = Wet IB 2001 as of 2026-02-21: https://wetten.overheid.nl/BWBR0011353/2026-02-21
- **WetIB25** = same law as of 2025-01-01: https://wetten.overheid.nl/BWBR0011353/2025-01-01
- **Stb195** = Staatsblad 2025, 195 (Wet tegenbewijsregeling box 3): https://zoek.officielebekendmakingen.nl/stb-2025-195.html
- **BD-WR** = Belastingdienst, Fiscale informatie 2025, ch. 31: https://www.belastingdienst.nl/wps/wcm/connect/fisin/fisin2025/Box_3_Werkelijk_rendement
- **BD-DB** = Belastingdienst, dividendbelasting: https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/winst/dividendbelasting/als_u_dividend_ontvangt/als_u_dividend_ontvangt
- **BVDB** = Besluit voorkoming dubbele belasting 2001: https://wetten.overheid.nl/BWBR0012095/2026-01-01
- **EK** = Eerste Kamer dossier 36.748: https://www.eerstekamer.nl/wetsvoorstel/36748_wet_werkelijk_rendement_box
- **KB** = cabinet letter, 30 Sep 2026: https://www.eerstekamer.nl/brief_in/20260930/voorstellen_op_box_3_koopkracht/f=/vn1fdr15xiz6.pdf
- **RO** = https://www.rijksoverheid.nl/themas/werk/inkomstenbelasting/plannen-werkelijk-rendement-box-3
- Secondary (flagged where used): Stolwijk https://stolwijkkennisnetwerk.nl/forfaits-box-3-2025-en-2026/ ; Infobak https://infobak.nl/financien/fictief-rendement-box-3/ ; Deloitte https://www.deloitte.com/nl/nl/services/tax/perspectives/kamerbrief-toekomst-box-3.html

Gaps: Belastingdienst parameter pages and the Staatscourant regeling for final 2025 percentages were not retrievable. The Wet overbrugging text was not read directly. Numbers come from the consolidated Wet IB.

## 1. Box 3 forfait, 2025–2026 [LAW]

- **Peildatum:** 1 January (WetIB art. 5.2 lid 1).
- **Categories** (art. 5.2 lid 3, 5.3): *banktegoeden* (deposits, cash); *overige bezittingen* (everything else, incl. shares, ETFs); *schulden*. Debts count only above **€3,800 per person / €7,600 partners** (art. 5.3 lid 3 f; 2026 text; 2025 amount [UNVERIFIED]).

| Year | Bank | Overige | Schulden |
|---|---|---|---|
| 2025 | 1.37% | 5.88% | 2.70% |
| 2026 | 1.28% provisional | 6.00% | 2.70% provisional |

- WetIB art. 5.2 lid 2 states "1,37% … 6% … 2,70%". 6% for overige is statutory, and 1.37%/2.70% match the 2025 final values.
- 2025 final status, 5.88%, and the provisional 2026 values (1.28%/2.70%) come only from Stolwijk and Infobak. [UNVERIFIED]
- Mechanism, art. 10.6ter: after year-end the bank % is replaced retroactively (DNB deposit rate, Jan–Nov, November counted twice, lid 2). The debt % is replaced the same way (DNB mortgage rate, lid 4). The overige % is set at the start of the year (53% property, 33% equity, 14% bond long-term returns, lid 3).
- So 2026 overige is **final**. 2026 bank and debt are **provisional** until early 2027. [INFERENCE from art. 10.6ter]
- **Heffingvrij vermogen** (art. 5.5): 2025 **€57,684** (WetIB25); 2026 **€59,357** (WetIB). Per person. Partners: joint base minus their combined heffingvrij (art. 5.2 lid 5), i.e. 2 × per person, €115,368 (2025) / €118,714 (2026). [INFERENCE; Infobak gives 2026 €118,714]
- **Rate:** 36% (art. 2.13, 2026 text). 2025 rate [UNVERIFIED]; Infobak says unchanged.

**Calculation order** (art. 5.2, 5.3):
1. Rendementsgrondslag = bezittingen − schulden (debts only above the threshold), at 1 Jan.
2. Rendement = bank×p_bank + overige×p_overig − schulden×p_schuld, floored at 0.
3. Effective % = rendement / rendementsgrondslag.
4. Grondslag sparen en beleggen = max(0, rendementsgrondslag − heffingvrij vermogen).
5. Voordeel = grondslag × effective %.
6. Tax = 36% × voordeel.

**Example 2026, single** (bank 1.28% provisional): bank €20,000, overige €80,000, no debt. Grondslag 100,000; rendement 256 + 4,800 = €5,056 (5.056%). Grondslag sparen en beleggen = 100,000 − 59,357 = 40,643. Voordeel ≈ €2,055. Tax ≈ **€740**.

## 2. Tegenbewijsregeling [LAW]

- **Basis:** Wet van 14 juli 2025, Stb 2025, 195. It adds Wet IB art. 5.25–5.36 and BVDB art. 6a.
- **Years:** artikel V of the wet makes artikel I and III retroactive to 1 Jan 2023, and artikel IV to 1 Jan 2017 (Stb195). The Wet IB provisions apply to aangifte 2025 (BD-WR). They hold "tot nieuwe wetgeving (waarschijnlijk 1 januari 2028)" (BD-WR). I read the 2023 date from a rendered image. [UNVERIFIED]
- **Rule** (art. 5.25): if the taxpayer makes it *aannemelijk* that actual return is lower than the forfait voordeel, taxable box 3 income = actual return (minus personal deductions). Actual return is at least nil (lid 2). BD-WR: you never pay more than under the forfait.
- **Definition** (art. 5.26–5.28): actual return = reguliere voordelen (rente, huur, pacht, **dividend**, art. 5.27) + vermogensaanwas. Vermogensaanwas = end value − start value − stortingen + onttrekkingen (art. 5.28).
- **Unrealised price changes count.** BD-WR: "de waardestijging of waardedaling van uw … aandelen" counts.
- **Costs are not deductible** (e.g. aan- en verkoopkosten). Exceptions: interest paid on box-3 debt, and a documented investment in a second home (BD-WR).
- **No heffingvrij vermogen and no debt threshold** (art. 5.26 lid 3; BD-WR).
- Gains and losses net within the year. A negative total becomes €0, with **no carry-forward** (BD-WR).
- In-year purchases count from the purchase date (BD-WR). Stortingen and onttrekkingen are valued at the time they occur (art. 5.29–5.30).
- **Form and process:** the taxpayer uses a form provided by the inspecteur (art. 5.25 lid 6). More documents may be filed up to six weeks after the form was sent (lid 7). The form requirement lapses if the inspecteur asked for the data with the aangifte invitation (lid 8). Using it is optional (BD-WR).
- For earlier years the form is "Opgaaf werkelijk rendement", after a Belastingdienst letter (https://www.rijksoverheid.nl/onderwerpen/inkomstenbelasting/box-3).
- For aangifte 2025 the claim can be made in the return itself (Stolwijk). [UNVERIFIED]
- I found no separate statutory deadline. [UNVERIFIED]

**Example 2025.** Portfolio €100,000 on 1 Jan, €10,000 deposited on 1 July, €105,000 on 31 Dec, no dividend (accumulating ETF).
- Forfait: grondslag 100,000 − 57,684 = 42,316 × 5.88% = €2,488; tax €896.
- Actual: 105,000 − 100,000 − 10,000 = −5,000, floored at 0, so tax **€0**. The rebuttal applies.
- With a 31 Dec value of €130,000, actual = +20,000, which is higher than the forfait voordeel. The forfait applies.

## 3. Dividends and foreign withholding

- **Dutch dividendbelasting:** 15%. A Dutch resident credits it against income tax and must declare the amount withheld in the aangifte (BD-DB). This applies to ASML.
- **Foreign withholding:** I saw only a search snippet of the Belastingdienst "Te verrekenen belasting" chapter. It reads: "U mag in uw aangifte alleen de Nederlandse dividendbelasting verrekenen." The sentence is cut off, so the full rule is [UNVERIFIED].
- BVDB art. 24 only gives a vermindering for foreign assets in box 3 where the other state also has the right to tax. It does not credit dividend withholding.
- Under the forfait, dividends are not box 3 income. No credit follows. [INFERENCE]
- Gross or net dividend, and whether foreign withholding is credited under the tegenbewijs, are [UNVERIFIED]. Art. 5.27 only says "dividend".
- Secondary sources say the excess over the treaty rate can be reclaimed from the source country. [UNVERIFIED]
- **Irish accumulating ETF (e.g. SXR8):**
  - No distribution reaches the investor, so there is no dividend to declare and no investor-level withholding.
  - Forfait: it is *overige bezittingen* at its 1 Jan value (art. 5.3 lid 2).
  - Tegenbewijs: retained income shows up as price change, which is vermogensaanwas (art. 5.28).
  - Fund-level US withholding, and whether it can be reclaimed, is [UNVERIFIED].

## 4. Wet werkelijk rendement box 3 (36.748) [PROPOSAL, not law]

- **Status** (EK):
  - Tweede Kamer passed it on 12 Feb 2026.
  - Plenary in the Eerste Kamer on 30 June 2026; the vote was deferred pending a novelle.
  - On 13 Oct 2026 the commission agreed "voorlopig" with the cabinet route and will wait for the Tweede Kamer's treatment of the novelle before deciding.
  - Entry into force: by koninklijk besluit. The aim is **1 Jan 2028** (EK; RO).
- **Original design** (EK; RO):
  - Vermogensaanwasbelasting is the main rule. Real income plus value changes are taxed yearly, so **unrealised gains on shares are taxed**. Costs are deductible.
  - Losses can be offset against later gains (RO).
  - Vermogenswinstbelasting (taxed on realisation) applies only to real estate and shares in startups.
  - Heffingsvrij resultaat €1,800 (Deloitte on KB; secondary).
  - The rate under the new system is [UNVERIFIED].
- **Cabinet proposal of 29/30 Sep 2026** (KB; Deloitte):
  - A novelle (ministerraad 2 Oct 2026, with the Raad van State for spoedadvies) puts **all financial instruments (shares, bonds, options) under vermogenswinstbelasting from 2028**. About 90% of assets would be taxed on realisation, not yearly.
  - Everything else follows in 2030.
  - The loss rule and the rate under the novelle are [UNVERIFIED].
  - For 2027: forfait overige from 6.37% to **7.87%**, and heffingvrij vermogen to **€30,846** (2026: €59,357). All of this is a proposal.
  - The cut of the heffingsvrij resultaat from €1,800 to €1,000 in 2028 reportedly lacks support in the Tweede Kamer (Deloitte).
  - The cabinet wants the novelle through the Eerste Kamer by 1 Dec 2026 (KB).

## 5. Practical: figures needed for the aangifte

- **Forfait:** value on 1 Jan per account and category, debts on 1 Jan, Dutch dividendbelasting withheld, plus dividends received.
- **Tegenbewijs, additionally:** 31 Dec values, all deposits and withdrawals with dates and values, dividends and interest received, interest paid.
- **Pitfalls:**
  - 2026 bank/debt percentages are provisional.
  - The forfait ignores trades during the year.
  - The tegenbewijs gives no cost deduction, counts unrealised changes, allows no loss carry-forward, and has no heffingvrij. A small portfolio with a small gain can do worse than under the forfait, because the forfait has the heffingvrij.
  - Dutch dividendbelasting is creditable. Foreign withholding is not shown to be. [UNVERIFIED]

## Implementable rules for the dashboard

**Inputs:** per person and year: 1 Jan values by category (bank / overige / debt); for tegenbewijs: 31 Dec value, dated deposits and withdrawals, dividends, interest received and paid; NL dividendbelasting withheld; foreign withholding; partner flag.

**Parameters:**

| Year | p_bank | p_overig | p_schuld | Heffingvrij pp | Debt threshold | Rate | Status |
|---|---|---|---|---|---|---|---|
| 2025 | 1.37% | 5.88% [UNV.] | 2.70% | €57,684 | €3,800 [UNV.] | 36% [UNV.] | final |
| 2026 | 1.28% | 6.00% | 2.70% | €59,357 | €3,800 | 36% | bank/debt provisional |
| 2027 | [UNV.] | 6.37% law, 7.87% proposal | [UNV.] | €59,357 or €30,846 (proposal) | [UNV.] | [UNV.] | proposal |
| 2028+ | – | – | – | – | – | – | proposal only; no formula |

Partners: heffingvrij ×2, debt threshold €7,600, joint base.

**Formulas:**
1. debt_eff = max(0, debt − threshold)
2. rg = bank + overig − debt_eff. If rg ≤ 0, tax = 0.
3. r = max(0, bank·p_bank + overig·p_overig − debt_eff·p_schuld)
4. forfait_tax = 0.36 × max(0, rg − heffingvrij) × (r / rg)
5. actual = max(0, V_end − V_start − deposits + withdrawals + dividends + interest − interest_paid). No costs deducted. No heffingvrij.
6. actual_tax = 0.36 × actual
7. tax = min(forfait_tax, actual_tax). Show both and which one applies.
8. Net payable = tax − NL dividendbelasting withheld. Foreign withholding is shown as information only, not credited.

**Show:** both results, inputs used, the percentages and their status, an aangifte checklist (section 5).

**Label as estimate:** any 2026 result (provisional bank/debt %), all 2027 figures, any 2028+ projection, foreign withholding and Irish-ETF fund-level tax.

Open: final 2025 regeling (Staatscourant); 2025 debt threshold and rate; foreign withholding under tegenbewijs; rate and loss rule under the novelle; a tegenbewijs filing deadline.
