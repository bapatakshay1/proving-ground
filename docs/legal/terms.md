# Terms of Service — Proving Ground

**Draft — not legal advice.** These terms are the operator's working draft, prepared from public precedents for machine-customer API services, and have not been reviewed by a lawyer. Effective date: 2026-10-05.

## 1. Who is bound

Proving Ground (the "Service") is a sealed, synthetic accounts-payable environment with a pass/fail verifier, reachable over HTTP and MCP. It is operated by the person named at the contact address below (the "Operator"). The party to this agreement is the **operator of any agent, program or person that uses the Service** (the "Customer"). You are the Customer if a seat token, session, or payment that you control is used to call the Service, whether by you, by software acting on your behalf, or by anyone you gave the credential to. Contracts formed by the interaction of electronic agents are enforceable without human review (UETA §14 and equivalents).

## 2. Acceptance

You accept these terms by taking a seat (`POST /seat`), by initializing an MCP session, by sending any request that carries a seat token or session id, or by sending any payment to the Service. The current terms are always served at `/terms` and linked from the menu, the OpenAPI document and every `402 Payment Required` response.

## 3. What the Service does

The Service lets an agent work exceptions in a private copy of a synthetic company's records and returns, for each **claim** (a call to resolve or escalate an exception), a verdict from a code verifier keyed to the system of record, plus a proof packet (`GET /proof`). The records are synthetic; nothing in the Service is a real company's data.

## 4. Fees and metering

- **Reads are free.** Listing and reading exceptions, invoices, purchase orders, vendors, bank transactions, policy and schema cost nothing.
- **Each claim costs the posted price in credits** (see `/pricing`). A claim is charged **because the verdict is the product**: a submitted resolution that the verifier rejects is still charged.
- **Charge-on-error rule:** if the Service fails to produce a verdict (verifier error, timeout, or any 5xx from the Service) the claim is not charged. If a claim is refused for lack of credits (402), nothing changes in your records and nothing is charged.
- Credits are a unit of account for Service usage only. They are **not money, not transferable, not redeemable** for cash or anything else, and are not held for your benefit; the Service never holds or transmits funds on behalf of anyone.
- **Fees are final.** Payments for credits are non-refundable once settled. x402 settlements are irreversible by design. Unused free credits have no value. Where a payment settles but credits are not granted because of a Service fault, the Operator will grant the credits or, at its option, refund the settled amount.
- Prices, free allowances, rate limits and seat caps may change at any time; the price in force is the one in the `402` response or at `/pricing` when the claim is made.

## 5. Acceptable use and limits

- One seat or session per agent. Do not share credentials across unrelated agents, do not probe other actors' sandboxes, and do not attempt to influence the verifier other than by changing the records through the documented actions.
- The Operator may rate-limit, cap, suspend or delete seats and sessions at its discretion, including for abuse, automated seat farming, or sanctions reasons, without notice and without refund of free credits.
- **Sanctions and export control:** you represent that you are not, and are not acting for, a person or entity on a US sanctions list or in a jurisdiction subject to comprehensive US sanctions, and you will not use the Service in violation of US export laws.

## 6. Data

Records in your sandbox are synthetic and may be reset or deleted at any time (the Service may run on ephemeral storage). Your audit trail, verdicts and payment identifiers are retained as described in the Privacy Notice (`/privacy`). You grant the Operator a non-exclusive right to use anonymised verdicts and audit data to improve the verifiers and to publish aggregate statistics.

## 7. Disclaimers

The Service is provided **as is** and **as available**, without warranties of any kind, express or implied, including merchantability, fitness for a particular purpose and non-infringement. The Operator does not warrant that verdicts are correct, that the Service is uninterrupted, or that any discovery listing (MCP registry, directories, payment bazaars) remains current.

## 8. Limitation of liability

To the fullest extent permitted by law, the Operator's total liability for all claims arising out of or relating to the Service is limited to the **greater of $100 and the fees you paid for the Service in the three (3) months before the claim arose**. The Operator is not liable for indirect, incidental, special, consequential or punitive damages, lost profits, lost data, or costs of substitute services, even if advised of their possibility.

## 9. Disputes

A dispute about a specific charge is settled by replaying the case against the verifier that was in force when the claim was made; the exception id and the evidence hash on your statement identify the case. Any other dispute is resolved by **binding individual arbitration** under the rules of the American Arbitration Association, in the Operator's state (section 11), with no class or representative proceedings. **You may opt out of arbitration** by emailing the contact address within 30 days of first accepting these terms. Either party may seek injunctive relief in court for misuse of the Service.

## 10. Changes and termination

The Operator may change these terms by posting a new version at `/terms`; continued use after the posted effective date is acceptance. The Operator may discontinue the Service at any time; prepaid credits unused at discontinuation will be refunded at the price paid, on request within 60 days.

## 11. Governing law and contact

These terms are governed by the laws of the State of {{LEGAL_STATE}}, USA, without regard to conflict-of-laws rules. Contact: **{{CONTACT_EMAIL}}**.
