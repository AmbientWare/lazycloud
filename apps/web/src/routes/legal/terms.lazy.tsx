import { createLazyFileRoute, Link } from "@tanstack/react-router";

import { LegalDocument, type LegalSection } from "../-marketing/LegalDocument";

export const Route = createLazyFileRoute("/legal/terms")({
  component: TermsOfService,
});

const EFFECTIVE_DATE = "September 3, 2026";
const LEGAL_EMAIL = "legal@lazycloud.dev";
const SUPPORT_EMAIL = "support@lazycloud.dev";

const sections = [
  {
    id: "agreement",
    title: "Agreement and eligibility",
    content: (
      <>
        <p>
          These Terms of Service are a binding agreement between you and LazyCloud. They govern your
          access to lazycloud.dev and the related websites, APIs, software, command-line tools,
          hosted compute, storage, networking, and support we provide. We call these the "Services."
        </p>
        <p>
          By creating an account, clicking to accept these Terms, or using the Services, you agree
          to these Terms and our <Link to="/legal/privacy">Privacy Policy</Link>. If you do not
          agree, do not use the Services.
        </p>
        <p>
          You must be at least 18 and legally able to enter this agreement. If you use the Services
          for a company or other organization, you represent that you have authority to bind it. In
          that case, "you" includes that organization.
        </p>
      </>
    ),
  },
  {
    id: "accounts",
    title: "Accounts and workspaces",
    content: (
      <>
        <p>
          You must provide accurate information, keep it current, and protect account credentials
          and access tokens. You are responsible for activity under your account and for the users
          you invite to a workspace. Tell us promptly if you suspect unauthorized access.
        </p>
        <p>
          Workspace owners and administrators can add or remove members, assign permissions, manage
          resources, and access workspace data according to their roles. If you join a workspace
          owned by an organization, that organization may control your access and the Customer
          Content in the workspace.
        </p>
        <p>
          You may not share credentials between people, misrepresent your identity, create accounts
          to evade restrictions, or transfer an account without our written consent.
        </p>
      </>
    ),
  },
  {
    id: "services",
    title: "The Services",
    content: (
      <>
        <p>
          Subject to these Terms and payment of applicable fees, LazyCloud grants you a limited,
          non-exclusive, non-transferable right to access and use the Services for your internal
          business or development purposes during the term.
        </p>
        <p>
          We may add, change, or discontinue features to operate, secure, or improve the Services.
          We will provide reasonable notice when a material change reduces paid functionality,
          unless an urgent security, legal, or infrastructure issue makes advance notice
          impractical. Preview, beta, and no-fee features may change or end at any time and are
          provided without a service-level commitment.
        </p>
        <p>
          Documentation describes how to use the Services but does not create a warranty or
          service-level commitment. Any separate order form or written agreement signed by you and
          LazyCloud controls if it expressly conflicts with these Terms.
        </p>
      </>
    ),
  },
  {
    id: "customer-content",
    title: "Customer Content",
    content: (
      <>
        <p>
          You retain your rights in code, files, inputs, outputs, logs, secrets, images, and other
          content submitted to the Services, which we call "Customer Content." You give LazyCloud a
          worldwide, non-exclusive license to host, copy, transmit, process, display, and modify
          Customer Content only as needed to provide, secure, support, and comply with law in
          connection with the Services. This license ends when the content is deleted from our
          systems, subject to normal backup rotation and legal retention duties.
        </p>
        <p>
          You are responsible for Customer Content and must have all rights and permissions needed
          for us to process it. You decide whether the Services are appropriate for regulated,
          confidential, personal, or sensitive data. Do not submit data subject to special legal or
          contractual restrictions unless the Services and your agreement with LazyCloud expressly
          support it.
        </p>
        <p>
          You are responsible for backups and for exporting Customer Content before deleting a
          resource, workspace, or account. Deleting compute, deployments, volumes, or workspaces may
          permanently delete associated content.
        </p>
      </>
    ),
  },
  {
    id: "acceptable-use",
    title: "Acceptable use",
    content: (
      <>
        <p>You will not use the Services, or help anyone else use them, to:</p>
        <ul>
          <li>violate law, sanctions, export controls, or another person's rights;</li>
          <li>
            upload or distribute malware, exploit code used without authorization, or content that
            infringes intellectual property, privacy, publicity, or other rights;
          </li>
          <li>
            gain unauthorized access to systems or data, probe or test systems without permission,
            or intercept communications;
          </li>
          <li>send spam, phishing, fraudulent, deceptive, harassing, or abusive communications;</li>
          <li>
            disrupt the Services, impose an unreasonable load, evade quotas or billing, or interfere
            with another customer's use;
          </li>
          <li>
            mine cryptocurrency without our prior written approval or use the Services to conceal
            the source of unlawful activity;
          </li>
          <li>
            reverse engineer or bypass technical restrictions, except where applicable law does not
            allow that restriction; or
          </li>
          <li>
            resell or provide the Services to third parties as a standalone hosting service without
            our written permission.
          </li>
        </ul>
        <p>
          We may investigate suspected violations and remove content, restrict traffic, quarantine
          workloads, or suspend access when reasonably necessary to protect the Services or others.
          We will give notice when practical and lawful.
        </p>
      </>
    ),
  },
  {
    id: "connected-infrastructure",
    title: "Connected infrastructure",
    content: (
      <>
        <p>
          The Services may let you run workloads in your own cloud account or on machines you join
          to LazyCloud. You authorize us to use the permissions and credentials you provide to
          configure, operate, meter, and remove resources you request.
        </p>
        <p>
          You remain responsible for your cloud-provider agreement, provider charges, account
          security, machine security, network configuration, capacity, licenses, and compliance.
          LazyCloud is not responsible for third-party outages, changes, charges, or data loss in
          infrastructure you control. Removing a connection does not necessarily cancel resources or
          charges held directly with the third-party provider, so you must confirm their status.
        </p>
      </>
    ),
  },
  {
    id: "fees-and-billing",
    title: "Fees and billing",
    content: (
      <>
        <p>
          Current plans, included usage, metered rates, and billing units appear on the{" "}
          <Link to="/pricing">pricing page</Link> or an order form. Resource use is measured by the
          Services. Your cloud provider separately bills resources in a connected cloud account.
        </p>
        <p>
          Paid plan fees are billed in advance on a recurring basis. Metered usage beyond an
          included amount is invoiced after use and charged to the payment method on file. When you
          add a payment method, you authorize LazyCloud and its payment provider to charge fees,
          usage, taxes, and other amounts you approve under these Terms.
        </p>
        <p>
          An upgrade may take effect immediately and include a prorated charge. A move to a lower
          priced plan takes effect for pricing at the next billing period, and the current period
          keeps the allowance already issued. Except where law or a signed order says otherwise,
          fees are non-refundable and credits have no cash value. You are responsible for taxes
          other than taxes on LazyCloud's income.
        </p>
        <p>
          We may correct billing errors. Email{" "}
          <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> about a billing dispute within 30
          days after the charge appears, with enough information for us to investigate. Failure to
          pay may result in workload stops, restricted storage operations, suspension, or
          termination. You remain responsible for fees incurred before suspension or termination.
        </p>
        <p>
          We may change prices prospectively. Changes to recurring plan fees take effect no earlier
          than your next billing period after notice. Changes to metered rates apply only to usage
          after the stated effective time.
        </p>
      </>
    ),
  },
  {
    id: "security",
    title: "Security and credentials",
    content: (
      <>
        <p>
          Each party will use reasonable safeguards for information it controls. You must configure
          workloads securely, grant only needed permissions, rotate exposed credentials, and keep
          secrets out of code, logs, and public endpoints. You may not disclose a LazyCloud access
          token except to a user or system authorized to act for the account.
        </p>
        <p>
          If you discover a vulnerability, report it responsibly and do not access, modify, or
          retain other customers' data. Security testing of LazyCloud requires prior written
          authorization.
        </p>
      </>
    ),
  },
  {
    id: "third-party-services",
    title: "Third-party services and software",
    content: (
      <>
        <p>
          The Services interoperate with third-party services such as GitHub, Stripe, cloud
          providers, domain providers, and software registries. Your use of those services is
          governed by their terms. LazyCloud does not control them and is not responsible for their
          acts, omissions, content, or availability.
        </p>
        <p>
          Software we distribute under an open-source license is governed by that license. If
          third-party software or content has separate license terms, those terms control your use
          of that item.
        </p>
      </>
    ),
  },
  {
    id: "ownership-and-feedback",
    title: "Ownership and feedback",
    content: (
      <>
        <p>
          LazyCloud and its licensors own the Services, including the software, design,
          documentation, trademarks, and related intellectual property, except for Customer Content
          and separately licensed software. These Terms grant no rights except the limited right to
          use the Services stated here.
        </p>
        <p>
          If you provide feedback, you give LazyCloud a perpetual, worldwide, irrevocable,
          royalty-free right to use it without restriction or payment. This does not give us rights
          to Customer Content included in the feedback beyond what is needed to review it.
        </p>
      </>
    ),
  },
  {
    id: "confidentiality",
    title: "Confidentiality",
    content: (
      <>
        <p>
          Each party may receive non-public information that a reasonable person would understand to
          be confidential. The receiving party will use it only to perform under these Terms and
          protect it with at least reasonable care. It may disclose confidential information only to
          personnel, contractors, and advisers who need it and are bound to protect it.
        </p>
        <p>
          Confidential information does not include information that becomes public without breach,
          was already lawfully known without restriction, is received lawfully from another source,
          or is independently developed. A party may disclose information when law requires it if,
          when legally allowed, it gives prompt notice and reasonable help seeking protection.
        </p>
      </>
    ),
  },
  {
    id: "term-and-termination",
    title: "Term, suspension, and termination",
    content: (
      <>
        <p>
          These Terms begin when you first accept them or use the Services and continue until
          terminated. You may stop using the Services at any time. You can manage a subscription
          through the dashboard and delete eligible workspaces there. A plan change or workspace
          deletion is complete only when the Services confirm it.
        </p>
        <p>
          We may suspend or terminate access if you materially breach these Terms, create a security
          or legal risk, fail to pay, or use the Services in a way that could harm LazyCloud, our
          customers, or third parties. We will give notice and a reasonable chance to cure when the
          circumstances allow. We may terminate a no-fee account or discontinue no-fee Services on
          reasonable notice.
        </p>
        <p>
          On termination, your right to use the Services ends. You remain responsible for accrued
          fees, and we may delete Customer Content according to our retention practices. Provisions
          that by their nature should survive will survive, including payment, ownership,
          confidentiality, disclaimers, liability limits, indemnity, disputes, and general terms.
        </p>
      </>
    ),
  },
  {
    id: "disclaimers",
    title: "Disclaimers",
    content: (
      <>
        <p>
          TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE SERVICES ARE PROVIDED "AS IS" AND "AS
          AVAILABLE." LAZYCLOUD DISCLAIMS ALL EXPRESS, IMPLIED, AND STATUTORY WARRANTIES, INCLUDING
          WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, NON-INFRINGEMENT,
          AND ANY WARRANTY ARISING FROM COURSE OF DEALING OR USAGE OF TRADE.
        </p>
        <p>
          We do not warrant that the Services will be uninterrupted, error-free, secure, or free of
          harmful components, or that Customer Content will never be lost. No service-level
          commitment applies unless it appears in a separate written agreement signed by LazyCloud.
          You are responsible for deciding whether the Services meet your requirements and for
          maintaining appropriate backups and recovery plans.
        </p>
        <p>
          Some jurisdictions do not allow certain warranty exclusions. Those exclusions apply only
          to the extent permitted by law.
        </p>
      </>
    ),
  },
  {
    id: "liability",
    title: "Limitation of liability",
    content: (
      <>
        <p>
          TO THE MAXIMUM EXTENT PERMITTED BY LAW, NEITHER PARTY WILL BE LIABLE FOR INDIRECT,
          INCIDENTAL, SPECIAL, EXEMPLARY, PUNITIVE, OR CONSEQUENTIAL DAMAGES, OR FOR LOST PROFITS,
          REVENUE, BUSINESS, GOODWILL, OR DATA, EVEN IF IT KNEW SUCH DAMAGES WERE POSSIBLE.
        </p>
        <p>
          EXCEPT FOR YOUR PAYMENT OBLIGATIONS, YOUR INDEMNITY OBLIGATIONS, OR A PARTY'S FRAUD,
          WILLFUL MISCONDUCT, OR INFRINGEMENT OF THE OTHER PARTY'S INTELLECTUAL PROPERTY RIGHTS,
          EACH PARTY'S TOTAL LIABILITY ARISING OUT OF OR RELATED TO THE SERVICES OR THESE TERMS WILL
          NOT EXCEED THE AMOUNT YOU PAID LAZYCLOUD FOR THE SERVICES DURING THE 12 MONTHS BEFORE THE
          EVENT GIVING RISE TO LIABILITY. IF YOU USED ONLY NO-FEE SERVICES, LAZYCLOUD'S TOTAL
          LIABILITY WILL NOT EXCEED US $100.
        </p>
        <p>
          These limits apply regardless of the form of action and even if a remedy fails its
          essential purpose. They do not limit liability that applicable law does not allow the
          parties to limit.
        </p>
      </>
    ),
  },
  {
    id: "indemnification",
    title: "Indemnification",
    content: (
      <p>
        You will defend, indemnify, and hold harmless LazyCloud and its personnel from third-party
        claims, damages, losses, and reasonable legal fees arising from Customer Content, your
        connected infrastructure, your products or services, your violation of these Terms or law,
        or your infringement of another person's rights. We will promptly notify you of a covered
        claim and provide reasonable cooperation. You may control the defense, but you may not
        settle a claim in a way that admits fault by or imposes obligations on LazyCloud without our
        written consent.
      </p>
    ),
  },
  {
    id: "disputes",
    title: "Governing law and disputes",
    content: (
      <>
        <p>
          These Terms are governed by California law, without regard to conflict-of-law rules. The
          Federal Arbitration Act governs the arbitration provisions below. The United Nations
          Convention on Contracts for the International Sale of Goods does not apply.
        </p>
        <h3>Informal resolution</h3>
        <p>
          Before filing a claim, the complaining party must send a written notice describing the
          dispute and requested relief. Send notices to{" "}
          <a href={`mailto:${LEGAL_EMAIL}`}>{LEGAL_EMAIL}</a>. The parties will try in good faith to
          resolve the dispute for 30 days after receipt.
        </p>
        <h3>Binding individual arbitration</h3>
        <p>
          PLEASE READ THIS PART CAREFULLY. EXCEPT FOR THE EXCEPTIONS BELOW, ANY DISPUTE ARISING OUT
          OF OR RELATING TO THESE TERMS OR THE SERVICES WILL BE RESOLVED BY BINDING ARBITRATION ON
          AN INDIVIDUAL BASIS, NOT IN COURT OR BEFORE A JURY.
        </p>
        <p>
          The American Arbitration Association will administer the arbitration under its Commercial
          Arbitration Rules, or its Consumer Arbitration Rules if they apply. One arbitrator will
          conduct the proceeding in English. The hearing may occur remotely unless the arbitrator
          requires otherwise. The arbitrator may award any relief a court could award to the
          individual parties and will issue a reasoned written decision.
        </p>
        <p>
          Neither party may bring or participate in a class, collective, consolidated, or
          representative action. The arbitrator may resolve only the individual claims before them.
          Either party may bring an eligible individual claim in small claims court or seek
          temporary injunctive relief in court to protect intellectual property, confidential
          information, systems, or data.
        </p>
        <h3>Arbitration opt-out</h3>
        <p>
          You may opt out of arbitration within 30 days after you first accept these Terms. Email
          <a href={`mailto:${LEGAL_EMAIL}`}> {LEGAL_EMAIL}</a> with the subject "Arbitration
          opt-out" and include your name and account email. Opting out does not change the rest of
          these Terms.
        </p>
        <p>
          For claims not subject to arbitration, each party consents to the exclusive jurisdiction
          of the state and federal courts located in San Francisco, California, unless applicable
          law requires a different forum.
        </p>
      </>
    ),
  },
  {
    id: "general",
    title: "General terms and contact",
    content: (
      <>
        <p>
          You will comply with applicable export-control and sanctions laws. You may not use the
          Services if law prohibits us from providing them to you. Neither party is liable for delay
          or failure caused by events beyond its reasonable control, except for payment obligations.
        </p>
        <p>
          You may not assign these Terms without our written consent. We may assign them in
          connection with a merger, acquisition, corporate reorganization, or sale of all or
          substantially all relevant assets. These Terms do not create a partnership, agency,
          employment, or joint venture. No third party is a beneficiary.
        </p>
        <p>
          If a provision is unenforceable, it will be modified only as much as needed and the rest
          will remain effective. A failure to enforce a provision is not a waiver. These Terms,
          together with the Privacy Policy, applicable order forms, and terms expressly incorporated
          here, are the entire agreement about the Services.
        </p>
        <p>
          We may update these Terms. Material changes take effect for existing users 30 days after
          notice unless law requires a different period. Other changes take effect when posted. By
          continuing to use the Services after the effective date, you accept the updated Terms.
        </p>
        <p>
          Send formal legal notices to LazyCloud at{" "}
          <a href={`mailto:${LEGAL_EMAIL}`}>{LEGAL_EMAIL}</a>. Send service and billing questions to{" "}
          <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>. We may send notices to the email
          address associated with your account or display them in the Services.
        </p>
      </>
    ),
  },
] as const satisfies readonly LegalSection[];

function TermsOfService() {
  return (
    <LegalDocument
      effectiveDate={EFFECTIVE_DATE}
      sections={sections}
      summary="The agreement that governs access to and use of LazyCloud."
      title="Terms of Service"
    />
  );
}
