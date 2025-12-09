"use client";

import { useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  AlertCircle,
  Shield,
  Lock,
  Eye,
} from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

const sections = [
  {
    id: "section-1",
    title: "1. Introduction",
    content: (
      <div className="space-y-4">
        <p>
          Thanks for entrusting LazyCloud with your projects and your personal
          information. Holding onto your private information is a serious
          responsibility, and we want you to know how we&apos;re handling it.
        </p>

        <h3 className="text-lg font-semibold">The short version</h3>
        <p>
          We collect your information only with your consent; we only collect
          the minimum amount of personal information that is necessary to
          fulfill the purpose of your interaction with us; we don&apos;t sell it
          to third parties; and we only use it as this Privacy Statement
          describes.
        </p>

        <p>
          Of course, the short version doesn&apos;t tell you everything, so
          please read on for more details!
        </p>
      </div>
    ),
  },
  {
    id: "section-2",
    title: "2. What information LazyCloud collects and why",
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">
          Information from website browsers
        </h3>
        <p>
          If you&apos;re just browsing the website, we collect the same basic
          information that most websites collect. We use common internet
          technologies, such as cookies and web server logs. This is stuff we
          collect from everybody, whether they have an account or not.
        </p>
        <p>
          The information we collect about all visitors to our website includes
          the visitor&apos;s browser type, language preference, referring site,
          additional websites requested, and the date and time of each visitor
          request. We also collect potentially personally-identifying
          information like Internet Protocol (IP) addresses.
        </p>

        <h3 className="text-lg font-semibold">Why do we collect this?</h3>
        <p>
          We collect this information to better understand how our website
          visitors use LazyCloud, and to monitor and protect the security of the
          website.
        </p>

        <h3 className="text-lg font-semibold">
          Information from users with accounts
        </h3>
        <p>
          If you create an account, we require some basic information at the
          time of account creation. We use WorkOS, a third-party authentication
          service, to manage user accounts and session management. You will
          create your own user name and password, and we will ask you for a
          valid email account. You also have the option to give us more
          information if you want to, and this may include &quot;User Personal
          Information.&quot;
        </p>
        <p>
          We offer sign-in through a variety of OAuth 2.0 providers such as
          Google and GitHub. When you choose to sign in using these providers,
          you authorize them to share certain information with us, which may
          include your name, email address, and profile picture. This
          information is used to create and manage your account on LazyCloud.
        </p>
        <p>
          &quot;User Personal Information&quot; is any information about one of
          our users which could, alone or together with other information,
          personally identify him or her. Information such as a user name and
          password, an email address, a real name, and a photograph are examples
          of &quot;User Personal Information.&quot;
        </p>
        <p>
          User Personal Information does not include aggregated, non-personally
          identifying information. We may use aggregated, non-personally
          identifying information to operate, improve, and optimize our website
          and service.
        </p>

        <h3 className="text-lg font-semibold">Why do we collect this?</h3>
        <ul className="list-disc space-y-2 pl-6">
          <li>
            We need your User Personal Information to create your account, and
            to provide the services you request.
          </li>
          <li>
            We use your User Personal Information, specifically your user name
            and email address, to identify you on LazyCloud.
          </li>
          <li>
            We will use your email address to communicate with you, if
            you&apos;ve said that&apos;s okay, and only for the reasons
            you&apos;ve said that&apos;s okay.
          </li>
          <li>
            We limit our use of your User Personal Information to the purposes
            listed in this Privacy Statement. If we need to use your User
            Personal Information for other purposes, we will ask your permission
            first.
          </li>
          <li>
            We use WorkOS to securely manage your authentication and session
            data, ensuring your account remains protected.
          </li>
        </ul>
      </div>
    ),
  },
  {
    id: "section-3",
    title: "3. What information LazyCloud does not collect",
    content: (
      <div className="space-y-4">
        <p>
          We do not intentionally collect sensitive personal information, such
          as social security numbers, genetic data, health information, or
          religious information. Although LazyCloud does not request or
          intentionally collect any sensitive personal information, we realize
          that you might store this kind of information in your account, such as
          in an application. If you store any sensitive personal information on
          our servers, you are consenting to our storage of that information on
          our servers, which are in the United States.
        </p>

        <p>
          We do not intentionally collect information that is stored in your
          applications or other free-form content inputs. Information in your
          applications belongs to you, and you are responsible for it, as well
          as for making sure that your content complies with our Terms of
          Service. LazyCloud employees do not access applications unless
          required to for security or maintenance, or for support reasons, with
          the consent of the application owner.
        </p>

        <p>
          If you&apos;re a child under the age of 13, you may not have an
          account on LazyCloud. LazyCloud does not knowingly collect information
          from or direct any of our content specifically to children under 13.
          If we learn or have reason to suspect that you are a user who is under
          the age of 13, we will unfortunately have to close your account. We
          don&apos;t want to discourage you from learning to code, but those are
          the rules. Please see our Terms of Service for information about
          account termination.
        </p>
      </div>
    ),
  },
  {
    id: "section-4",
    title: "4. How we share the information we collect",
    content: (
      <div className="space-y-4">
        <p>
          We do not share, sell, rent, or trade User Personal Information with
          third parties for their commercial purposes.
        </p>

        <p>
          We do not disclose User Personal Information outside LazyCloud, except
          in the situations listed in this section or in the section below on
          Compelled Disclosure.
        </p>

        <p>
          We do share certain aggregated, non-personally identifying information
          with others about how our users, collectively, use LazyCloud, or how
          our users respond to our other offerings, such as our conferences or
          events. For example, we may compile statistics on the usage of HTTP
          content types across LazyCloud. However, we do not sell this
          information to advertisers or marketers.
        </p>

        <p>
          We do not host advertising on LazyCloud. We may occasionally embed
          content from third party sites, such as YouTube, and that content may
          include ads. While we try to minimize the amount of ads our embedded
          content contains, we can&apos;t always control what third parties
          show.
        </p>

        <p>
          We may share User Personal Information with your permission, so we can
          perform services you have requested.
        </p>

        <p>
          We may share User Personal Information with a limited number of
          third-party vendors who process it on our behalf to provide or improve
          our service, and who have agreed to privacy restrictions similar to
          our own Privacy Statement. Our vendors perform services such as
          payment processing, customer support ticketing, network data
          transmission, and other similar services.
        </p>

        <p>
          We use WorkOS as a third-party authentication service to manage user
          accounts and session data. When you sign up or sign in to LazyCloud,
          WorkOS processes your authentication information in accordance with
          their own privacy policy. We recommend reviewing WorkOS&apos;s privacy
          policy to understand how they handle your data.
        </p>

        <p>
          We may share User Personal Information if we are involved in a merger,
          sale, or acquisition. If any such change of ownership happens, we will
          ensure that it is under terms that preserve the confidentiality of
          User Personal Information, and we will notify you on our website or by
          email before any transfer of your User Personal Information. The
          organization receiving any User Personal Information will have to
          honor any promises we have made in our Privacy Statement or in our
          Terms of Service.
        </p>
      </div>
    ),
  },
  {
    id: "section-5",
    title: "5. Our use of cookies and tracking",
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">Cookies</h3>
        <p>
          LazyCloud uses cookies to make interactions with our service easy and
          meaningful. We use cookies (and similar technologies, like HTML5
          localStorage) to keep you logged in, remember your preferences, and
          provide information for future development of LazyCloud.
        </p>
        <p>
          A cookie is a small piece of text that our web server stores on your
          computer or mobile device, which your browser sends to us when you
          return to our site. Cookies do not necessarily identify you if you are
          merely visiting LazyCloud; however, a cookie may store a unique
          identifier for each logged in user. The cookies LazyCloud sets are
          essential for the operation of the website, or are used for
          performance or functionality. By using our website, you agree that we
          can place these types of cookies on your computer or device. If you
          disable your browser or device&apos;s ability to accept cookies, you
          will not be able to log in or use LazyCloud&apos;s services.
        </p>
        <p>
          Our authentication service, WorkOS, also uses cookies and similar
          technologies to manage your session and authentication state. These
          cookies are essential for the secure operation of your account and
          cannot be disabled if you wish to use LazyCloud&apos;s services.
        </p>

        <h3 className="text-lg font-semibold">Google Analytics</h3>
        <p>
          We use Google Analytics as a third party tracking service, but we
          don&apos;t use it to track you individually or collect your User
          Personal Information. We use Google Analytics to collect information
          about how our website performs and how our users, in general, navigate
          through and use LazyCloud. This helps us evaluate our users&apos; use
          of LazyCloud; compile statistical reports on activity; and improve our
          content and website performance.
        </p>
        <p>
          Google Analytics gathers certain simple, non-personally identifying
          information over time, such as your IP address, browser type, internet
          service provider, referring and exit pages, time stamp, and similar
          data about your use of LazyCloud. We do not link this information to
          any of your personal information such as your user name.
        </p>
        <p>
          LazyCloud will not, nor will we allow any third party to, use the
          Google Analytics tool to track our users individually; collect any
          User Personal Information other than IP address; or correlate your IP
          address with your identity. Google provides further information about
          its own privacy practices and offers a browser add-on to opt out of
          Google Analytics tracking.
        </p>

        <h3 className="text-lg font-semibold">Tracking</h3>
        <p>
          &quot;Do Not Track&quot; is a privacy preference you can set in your
          browser if you do not want online services to collect and share
          certain kinds of information about your online activity from third
          party tracking services. We do not track your online browsing activity
          on other online services over time and we do not permit third-party
          services to track your activity on our site beyond our basic Google
          Analytics tracking, which you may opt out of here. Because we do not
          share this kind of data with third party services or permit this kind
          of third party data collection on LazyCloud for any of our users, and
          we do not track our users on third-party websites ourselves, we do not
          need to respond differently to an individual browser&apos;s Do Not
          Track setting.
        </p>
        <p>
          If you are interested in turning on your browser&apos;s privacy and Do
          Not Track settings, the Do Not Track website has browser-specific
          instructions.
        </p>
      </div>
    ),
  },
  {
    id: "section-6",
    title: "6. How LazyCloud secures your information",
    content: (
      <div className="space-y-4">
        <p>
          LazyCloud takes all measures reasonably necessary to protect User
          Personal Information from unauthorized access, alteration, or
          destruction; maintain data accuracy; and help ensure the appropriate
          use of User Personal Information. We follow generally accepted
          industry standards to protect the personal information submitted to
          us, both during transmission and once we receive it.
        </p>
        <p>
          No method of transmission, or method of electronic storage, is 100%
          secure. Therefore, we cannot guarantee its absolute security. For more
          information, see our security disclosures.
        </p>
      </div>
    ),
  },
  {
    id: "section-7",
    title: "7. LazyCloud&apos;s global privacy practices",
    content: (
      <div className="space-y-4">
        <p>
          Information that we collect will be stored and processed in the United
          States in accordance with this Privacy Statement. However, we
          understand that we have users from different countries and regions
          with different privacy expectations, and we try to meet those needs.
        </p>
        <p>
          We provide the same standard of privacy protection to all our users
          around the world, regardless of their country of origin or location,
          and we are proud of the levels of notice, choice, accountability,
          security, data integrity, access, and recourse we provide. We have
          appointed a Privacy Counsel and we work hard to comply with the
          applicable data privacy laws wherever we do business. Additionally, we
          require that if our vendors or affiliates have access to User Personal
          Information, they must comply with our privacy policies and with
          applicable data privacy laws, including signing data transfer
          agreements such as Standard Contractual Clause agreements.
        </p>
        <p>In particular:</p>
        <ul className="list-disc space-y-2 pl-6">
          <li>
            LazyCloud provides clear methods of unambiguous, informed consent at
            the time of data collection, when we do collect your personal data.
          </li>
          <li>
            We collect only the minimum amount of personal data necessary,
            unless you choose to provide more. We encourage you to only give us
            the amount of data you are comfortable sharing.
          </li>
          <li>
            We offer you simple methods of accessing, correcting, or deleting
            the data we have collected.
          </li>
          <li>
            We provide our users notice, choice, accountability, security, and
            access, and we limit the purpose for processing. We also provide our
            users a method of recourse and enforcement.
          </li>
        </ul>
      </div>
    ),
  },
  {
    id: "section-8",
    title: "8. Resolving Complaints",
    content: (
      <div className="space-y-4">
        <p>
          If you have concerns about the way LazyCloud is handling your User
          Personal Information, please let us know immediately. We want to help.
          You may{" "}
          <a href="/support" className="text-lazycloud hover:underline">
            contact support
          </a>{" "}
          with the subject line &quot;Privacy Concerns.&quot; We will respond within 45 days at
          the latest.
        </p>
      </div>
    ),
  },
  {
    id: "section-9",
    title: "9. How we respond to compelled disclosure",
    content: (
      <div className="space-y-4">
        <p>
          LazyCloud may disclose personally-identifying information or other
          information we collect about you to law enforcement in response to a
          valid subpoena, court order, warrant, or similar government order, or
          when we believe in good faith that disclosure is reasonably necessary
          to protect our property or rights, or those of third parties or the
          public at large.
        </p>
        <p>
          In complying with court orders and similar legal processes, LazyCloud
          strives for transparency. When permitted, we will make a reasonable
          effort to notify users of any disclosure of their information, unless
          we are prohibited by law or court order from doing so, or in rare,
          exigent circumstances.
        </p>
      </div>
    ),
  },
  {
    id: "section-10",
    title: "10. How you can access and control the information we collect",
    content: (
      <div className="space-y-4">
        <p>
          If you&apos;re already a LazyCloud user, you may access, update,
          alter, or delete your basic user profile information by editing your
          user profile or{" "}
          <a href="/support" className="text-lazycloud hover:underline">
            contacting support
          </a>
          .
        </p>

        <h3 className="text-lg font-semibold">Data Retention and Deletion</h3>
        <p>
          LazyCloud will retain User Personal Information for as long as your
          account is active or as needed to provide you services.
        </p>
        <p>
          We may retain certain User Personal Information indefinitely, unless
          you delete it or request its deletion. For example, we don&apos;t
          automatically delete inactive user accounts, so unless you choose to
          delete your account, we will retain your account information
          indefinitely.
        </p>
        <p>
          If you would like to cancel your account or delete your User Personal
          Information, you may do so by{" "}
          <a href="/support" className="text-lazycloud hover:underline">
            contacting support
          </a>
          . We will
          retain and use your information as necessary to comply with our legal
          obligations, resolve disputes, and enforce our agreements, but barring
          legal requirements, we will delete your full profile (within reason)
          within 30 days.
        </p>
        <p>
          Please note that if you signed up using an OAuth 2.0 provider like
          Google or GitHub, you may need to manage your account permissions
          through those services as well. Deleting your LazyCloud account does
          not automatically revoke access granted to LazyCloud through these
          third-party services.
        </p>

        <h3 className="text-lg font-semibold">Public Data Privacy Framework</h3>
        <p>
          LazyCloud complies with the EU-U.S. Data Privacy Framework, the UK
          Extension to the EU-U.S. Data Privacy Framework, and the Swiss-U.S.
          Data Privacy Framework (collectively, &quot;DPF&quot; and the personal
          information collected in reliance on the DPF is &quot;European
          Personal Information&quot;) as set forth by the U.S. Department of
          Commerce. LazyCloud has certified to the U.S. Department of Commerce
          that it adheres to the DPF Principles with regard to the processing of
          European Personal Information. If there is any conflict between the
          terms in this Privacy Statement and the DPF Principles, the DPF
          Principles shall govern. To learn more about the DPF program, and to
          view our certification, please visit Data Privacy Framework website.
        </p>
      </div>
    ),
  },
  {
    id: "section-11",
    title: "11. How we communicate with you",
    content: (
      <div className="space-y-4">
        <p>
          We will use your email address to communicate with you, if you&apos;ve
          said that&apos;s okay, and only for the reasons you&apos;ve said
          that&apos;s okay. You have a lot of control over how your email
          address is used and shared on and through LazyCloud.
        </p>
        <p>
          Depending on your email settings, LazyCloud may occasionally send
          notification emails about new features, requests for feedback,
          important policy changes, or offer customer support. We also send
          marketing emails, but only with your consent. There&apos;s an
          unsubscribe link located at the bottom of each of the emails we send
          you.
        </p>
        <p>
          Our emails might contain a pixel tag, which is a small, clear image
          that can tell us whether or not you have opened an email and what your
          IP address is. We use this pixel tag to make our email more effective
          for you and to make sure we&apos;re not sending you unwanted email. If
          you prefer not to receive pixel tags, please opt out of marketing
          emails.
        </p>
      </div>
    ),
  },
  {
    id: "section-12",
    title: "12. Changes to our Privacy Statement",
    content: (
      <div className="space-y-4">
        <p>
          Although most changes are likely to be minor, LazyCloud may change our
          Privacy Statement from time to time. We will provide notification to
          Users of material changes to this Privacy Statement through our
          Website at least 30 days prior to the change taking effect by posting
          a notice on our home page or sending email to the email address
          specified in your LazyCloud primary account. For changes to this
          Privacy Statement that do not affect your rights, we encourage
          visitors to check this page frequently.
        </p>
      </div>
    ),
  },
  {
    id: "section-13",
    title: "13. Contacting LazyCloud",
    content: (
      <div className="space-y-4">
        <p>
          Questions regarding LazyCloud&apos;s Privacy Statement or information
          practices should be directed to{" "}
          <a href="/support" className="text-lazycloud hover:underline">
            support
          </a>
          .
        </p>
      </div>
    ),
  },
];

export default function PrivacyPage() {
  const [expandAll, setExpandAll] = useState(false);
  const effectiveDate = new Date().toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  });

  const toggleAll = () => {
    setExpandAll(!expandAll);
  };

  return (
    <main className="flex min-h-screen flex-1 overflow-hidden px-4 pt-20 pb-8 sm:px-6 sm:pt-24 lg:px-8">
      <div className="bg-card border-border/50 mx-auto flex w-full max-w-5xl flex-1 flex-col overflow-hidden rounded-xl border p-6 shadow-sm sm:p-8 lg:p-10">
        <div className="mb-12 text-center">
          <h1 className="mb-3 text-3xl font-bold tracking-tight sm:text-4xl md:text-5xl">
            Privacy Policy
          </h1>
          <p className="text-muted-foreground text-sm">
            Effective date: {effectiveDate}
          </p>
        </div>

        <Card className="border-border/50 mb-8 shadow-sm">
          <CardContent className="p-6">
            <div className="flex flex-col items-start gap-6 md:flex-row md:items-center">
              <div className="bg-muted flex-shrink-0 rounded-full p-3">
                <AlertCircle className="h-6 w-6 text-amber-500" />
              </div>
              <div className="flex-1 space-y-2">
                <h2 className="text-lg font-semibold">Important Notice</h2>
                <p className="text-muted-foreground text-sm">
                  By using LazyCloud, you agree to this Privacy Policy. Please
                  read it carefully to understand how we collect, use, and
                  protect your personal information.
                </p>
              </div>
              <div className="mt-4 md:mt-0 md:ml-auto">
                <Button
                  onClick={toggleAll}
                  variant="outline"
                  className="flex items-center gap-2"
                >
                  {expandAll ? (
                    <>
                      <ChevronUp className="h-4 w-4" />
                      <span>Collapse All</span>
                    </>
                  ) : (
                    <>
                      <ChevronDown className="h-4 w-4" />
                      <span>Expand All</span>
                    </>
                  )}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        <div className="prose prose-slate dark:prose-invert mb-8 max-w-none">
          <p className="text-foreground pb-4 text-lg">
            Thank you for using LazyCloud! We&apos;re happy you&apos;re here.
            Please read this Privacy Policy carefully before accessing or using
            LazyCloud.
          </p>
          <p className="text-muted-foreground">
            This Privacy Policy describes how LazyCloud collects, uses, and
            shares your personal information when you use our website located at
            lazycloud.dev and its subdomains (collectively, the
            &quot;Website&quot;) and the products, services, content and other
            resources available on or enabled via our Website (collectively, the
            &quot;LazyCloud Services&quot;).
          </p>
        </div>

        <div className="mb-12 grid grid-cols-1 gap-6 md:grid-cols-3">
          <div className="bg-muted/30 border-border/50 rounded-lg border p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className="rounded-full bg-emerald-100 p-2 dark:bg-emerald-900/30">
                <Shield className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
              </div>
              <h3 className="text-lg font-semibold">Data Protection</h3>
            </div>
            <p className="text-muted-foreground text-sm">
              We take your data security seriously and implement
              industry-standard protections to keep your information safe.
            </p>
          </div>
          <div className="bg-muted/30 border-border/50 rounded-lg border p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className="rounded-full bg-blue-100 p-2 dark:bg-blue-900/30">
                <Lock className="h-5 w-5 text-blue-600 dark:text-blue-400" />
              </div>
              <h3 className="text-lg font-semibold">Your Control</h3>
            </div>
            <p className="text-muted-foreground text-sm">
              You have control over your personal information and can access,
              update, or delete it at any time.
            </p>
          </div>
          <div className="bg-muted/30 border-border/50 rounded-lg border p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className="rounded-full bg-purple-100 p-2 dark:bg-purple-900/30">
                <Eye className="h-5 w-5 text-purple-600 dark:text-purple-400" />
              </div>
              <h3 className="text-lg font-semibold">Transparency</h3>
            </div>
            <p className="text-muted-foreground text-sm">
              We&apos;re transparent about how we collect, use, and share your
              information, and we don&apos;t sell your data to third parties.
            </p>
          </div>
        </div>

        <Accordion
          type="multiple"
          defaultValue={expandAll ? sections.map((s) => s.id) : []}
          value={expandAll ? sections.map((s) => s.id) : undefined}
          className="mb-12 space-y-2"
        >
          {sections.map((section) => (
            <AccordionItem
              key={section.id}
              value={section.id}
              className="border-border/50 bg-muted/20 rounded-lg px-4"
            >
              <AccordionTrigger className="py-4 text-xl font-semibold hover:no-underline">
                {section.title}
              </AccordionTrigger>
              <AccordionContent className="text-muted-foreground pb-6">
                {section.content}
              </AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>

        <div className="bg-muted/30 border-border/50 mb-12 rounded-xl border p-6">
          <h2 className="mb-4 text-xl font-bold">Contact Us</h2>
          <p className="text-muted-foreground mb-4">
            If you have any questions about this Privacy Policy or if you wish
            to make any complaint or claim with respect to the Services, please{" "}
            <a
              href="/support"
              className="text-lazycloud font-medium hover:underline"
            >
              contact support
            </a>
            .
          </p>
        </div>

        <div className="text-muted-foreground text-center text-sm">
          <p>© {new Date().getFullYear()} LazyCloud. All rights reserved.</p>
        </div>
      </div>
    </main>
  );
}
