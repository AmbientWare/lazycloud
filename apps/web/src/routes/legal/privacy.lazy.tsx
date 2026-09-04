import { createLazyFileRoute } from "@tanstack/react-router";

import { LegalDocument, type LegalSection } from "../-marketing/LegalDocument";

export const Route = createLazyFileRoute("/legal/privacy")({
  component: PrivacyPolicy,
});

const EFFECTIVE_DATE = "September 3, 2026";
const PRIVACY_EMAIL = "privacy@lazycloud.dev";

const sections = [
  {
    id: "scope",
    title: "Scope and our role",
    content: (
      <>
        <p>
          This Privacy Policy explains how LazyCloud collects, uses, discloses, and protects
          personal information when you visit lazycloud.dev, create an account, or use our websites,
          APIs, command-line tools, hosted compute, and related services. We call these the
          "Services."
        </p>
        <p>
          LazyCloud controls the personal information used to operate accounts, billing, security,
          and the Services. A customer controls the code, files, inputs, outputs, logs, and other
          data submitted to its workspaces, which we call "Customer Content." When Customer Content
          contains personal information, we process it for the customer under our agreement with
          that customer. If you want to exercise rights over information a LazyCloud customer
          controls, contact that customer first.
        </p>
        <p>
          This policy does not cover third-party products or services that have their own privacy
          policies, including services you connect to LazyCloud or workloads you operate outside
          LazyCloud.
        </p>
      </>
    ),
  },
  {
    id: "information-we-collect",
    title: "Information we collect",
    content: (
      <>
        <h3>Account information</h3>
        <p>
          When you sign in with GitHub, we receive your GitHub account identifier, username, display
          name, verified primary email address, profile image URL, and GitHub account creation date.
          We also keep workspace memberships, roles, access tokens, and account settings needed to
          provide the Services.
        </p>

        <h3>Customer Content</h3>
        <p>
          We process content you or your authorized users submit to the Services. This can include
          source code, container images, build context, commands, function arguments and results,
          application traffic, logs, secrets, files, volume data, queue and map data, and support
          materials. You decide what Customer Content to submit, and it may include personal or
          sensitive information.
        </p>

        <h3>Service and integration data</h3>
        <p>
          We collect data about workloads, resource use, deployments, storage, domains, network
          activity, errors, and administrative actions. If you connect an AWS account, a machine, a
          domain, or another service, we process the identifiers, configuration, permissions, and
          operational data needed to run that connection.
        </p>

        <h3>Billing information</h3>
        <p>
          We keep billing contact details, plan and subscription status, usage records, invoice
          information, and payment-provider identifiers. Our payment provider collects and stores
          payment card details on its hosted pages. Full card numbers do not pass through or stay on
          LazyCloud systems.
        </p>

        <h3>Device, network, and communications data</h3>
        <p>
          When you use the Services, we may collect IP address, browser and device details, request
          time, requested route, referring page, request identifiers, and security and diagnostic
          logs. We also keep information you send when you contact us, report abuse, or request
          support.
        </p>
      </>
    ),
  },
  {
    id: "sources",
    title: "Where information comes from",
    content: (
      <>
        <p>We collect personal information from:</p>
        <ul>
          <li>you, when you register, configure, pay for, or use the Services;</li>
          <li>GitHub, when you choose GitHub sign-in;</li>
          <li>your organization and its workspace administrators;</li>
          <li>services and infrastructure you connect to LazyCloud; and</li>
          <li>our systems and service providers as they operate and secure the Services.</li>
        </ul>
      </>
    ),
  },
  {
    id: "how-we-use-information",
    title: "How we use information",
    content: (
      <>
        <p>We use personal information to:</p>
        <ul>
          <li>create and administer accounts, workspaces, and access permissions;</li>
          <li>run workloads, store content, route traffic, and provide requested features;</li>
          <li>measure resource use, manage subscriptions, invoice charges, and prevent fraud;</li>
          <li>authenticate users and protect customers, LazyCloud, and the public from abuse;</li>
          <li>diagnose failures, maintain reliability, and improve the Services;</li>
          <li>respond to questions, support requests, and legal notices; and</li>
          <li>meet legal obligations and enforce our agreements.</li>
        </ul>
        <p>
          Where the law requires a legal basis, we rely on performance of our contract, our
          legitimate interests in operating and protecting the Services, compliance with law, and
          consent where we ask for it. You may withdraw consent at any time, but withdrawal does not
          affect processing that already occurred.
        </p>
        <p>
          We may create aggregated or de-identified information and use it to understand capacity,
          reliability, and product use. We do not try to identify people from information that has
          been de-identified.
        </p>
      </>
    ),
  },
  {
    id: "how-we-disclose-information",
    title: "How we disclose information",
    content: (
      <>
        <p>We may disclose personal information to:</p>
        <ul>
          <li>
            vendors that provide cloud infrastructure, storage, networking, authentication,
            payments, communications, monitoring, security, and professional services;
          </li>
          <li>
            workspace owners, administrators, and members according to their roles and the actions
            they take in a shared workspace;
          </li>
          <li>
            third parties you direct us to use, including connected cloud accounts, machines,
            domains, and applications;
          </li>
          <li>
            law enforcement, regulators, courts, or other parties when reasonably necessary to
            comply with law, protect rights and safety, or investigate fraud or abuse; and
          </li>
          <li>
            a buyer, investor, adviser, or successor in connection with a financing, reorganization,
            merger, acquisition, or sale of assets, subject to appropriate confidentiality
            protections.
          </li>
        </ul>
        <p>
          GitHub processes the sign-in information you authorize it to provide. Stripe processes
          payment information and hosts payment and billing-management pages. Their handling of
          information is also governed by their own privacy notices.
        </p>
        <p>
          We do not sell personal information. We do not share personal information for
          cross-context behavioral advertising, and we do not use personal information for targeted
          advertising.
        </p>
      </>
    ),
  },
  {
    id: "cookies-and-local-storage",
    title: "Cookies and local storage",
    content: (
      <>
        <p>
          LazyCloud uses a short-lived, secure cookie to complete GitHub sign-in. After sign-in, the
          dashboard stores a session credential in your browser's local storage so it can
          authenticate requests. Removing that credential signs the browser out.
        </p>
        <p>
          We do not use advertising cookies on the Services as of the effective date above. Your
          browser may let you remove cookies and local storage, but doing so can interrupt sign-in
          or sign you out. Third-party sites you visit through a link may set their own cookies.
        </p>
      </>
    ),
  },
  {
    id: "retention",
    title: "Retention and deletion",
    content: (
      <>
        <p>
          We keep personal information only as long as needed for the purposes described in this
          policy. The period depends on the type of information, the customer's configuration and
          actions, operational needs, and legal requirements.
        </p>
        <p>
          Account and workspace records generally remain while the account or workspace is active.
          Customer Content remains until the customer deletes it, a configured retention rule
          removes it, or the related workspace is deleted. Some build artifacts, checkpoints, logs,
          and temporary operational data expire sooner. Billing, security, audit, backup, and
          dispute records may remain longer when needed for legal compliance, fraud prevention,
          accounting, or the establishment and defense of legal claims.
        </p>
        <p>
          Deletion from active systems may not remove information immediately from backups. Backup
          copies are removed on their normal rotation schedule unless the law requires longer
          retention.
        </p>
      </>
    ),
  },
  {
    id: "security",
    title: "Security",
    content: (
      <>
        <p>
          We use administrative, technical, and physical safeguards designed to protect personal
          information. These include access controls, tenant separation, encryption in transit,
          credential protections, logging, and scoped permissions for connected infrastructure.
        </p>
        <p>
          No system is perfectly secure. You are responsible for protecting your credentials,
          choosing appropriate permissions, keeping backups where needed, and promptly telling us if
          you suspect unauthorized access.
        </p>
      </>
    ),
  },
  {
    id: "international-transfers",
    title: "International data transfers",
    content: (
      <>
        <p>
          LazyCloud and its service providers may process information in the United States and other
          countries where we or they operate. Customer Content may also be processed where your
          selected compute, connected cloud account, or joined machine runs. Those countries may
          have different data-protection laws from your country.
        </p>
        <p>
          When applicable law requires a transfer mechanism, we use recognized safeguards such as
          contractual protections. You are responsible for choosing workload locations suitable for
          the Customer Content you submit.
        </p>
      </>
    ),
  },
  {
    id: "privacy-rights",
    title: "Your privacy rights",
    content: (
      <>
        <p>
          Depending on where you live, you may have the right to ask for access to, correction of,
          deletion of, or a portable copy of your personal information. You may also have rights to
          restrict or object to processing, withdraw consent, appeal a decision, and opt out of
          certain sale, sharing, targeted-advertising, or profiling activities. We do not
          discriminate against you for exercising a privacy right.
        </p>
        <p>
          Send a request to <a href={`mailto:${PRIVACY_EMAIL}`}>{PRIVACY_EMAIL}</a>. Describe the
          right you want to exercise and the account involved. We may need to verify your identity
          and authority before acting. An authorized agent may submit a request where the law
          permits, but we may ask for proof of authorization. To appeal a decision, reply to our
          decision and write "Privacy appeal" in the subject line.
        </p>
        <p>
          These rights have exceptions. We may keep information needed to provide a service you
          requested, protect security, comply with law, or establish and defend legal claims. If
          Customer Content is controlled by your organization, we will direct the request to that
          organization or help it respond as required by our agreement.
        </p>
        <h3>Additional information for United States residents</h3>
        <p>
          In the preceding 12 months, we collected the categories described above: identifiers and
          account information, commercial and billing information, internet and network activity,
          service and integration data, communications, and Customer Content. Customer Content and
          account credentials may include information treated as sensitive under some laws. We use
          sensitive information only to provide and protect the Services or as you direct, not to
          infer characteristics about you.
        </p>
        <p>
          We disclosed relevant categories to service providers, workspace members, user-directed
          integrations, and legal or transaction recipients for the purposes described in this
          policy. We did not sell these categories or share them for cross-context behavioral
          advertising in the preceding 12 months.
        </p>
      </>
    ),
  },
  {
    id: "children",
    title: "Children",
    content: (
      <p>
        The Services are not directed to children under 18, and we do not knowingly collect their
        personal information. If you believe a child has provided personal information to us,
        contact us so we can investigate and delete it where required.
      </p>
    ),
  },
  {
    id: "changes-and-contact",
    title: "Changes and contact",
    content: (
      <>
        <p>
          We may update this policy as the Services or law changes. We will post the updated policy
          here and revise the effective date. If a change materially affects your rights, we will
          provide additional notice where required.
        </p>
        <p>
          Questions or privacy requests can be sent to LazyCloud at{" "}
          <a href={`mailto:${PRIVACY_EMAIL}`}>{PRIVACY_EMAIL}</a>.
        </p>
      </>
    ),
  },
] as const satisfies readonly LegalSection[];

function PrivacyPolicy() {
  return (
    <LegalDocument
      effectiveDate={EFFECTIVE_DATE}
      sections={sections}
      summary="How LazyCloud handles personal information when you use the site and Services."
      title="Privacy Policy"
    />
  );
}
