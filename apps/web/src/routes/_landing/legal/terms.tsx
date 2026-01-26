import { useState } from 'react'
import { createFileRoute } from '@tanstack/react-router'
import {
  ChevronDown,
  ChevronUp,
  FileText,
  Shield,
  Clock,
  AlertCircle,
} from 'lucide-react'
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import FeatureCard from '../-components/legal/feature-card'

export const Route = createFileRoute('/_landing/legal/terms')({
  component: TermsPage,
})

const sections = [
  {
    id: 'section-1',
    title: '1. LazyCloud Services',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">
          Subscription to the LazyCloud Services
        </h3>
        <p>
          Subject to the terms and conditions of this Agreement, LazyCloud
          hereby grants to Customer, during the applicable Service Period (as
          defined in Section 13), a non-sublicensable, non-transferable,
          non-exclusive subscription to, solely for Customer&apos;s internal
          use: (a) access and use the applicable LazyCloud Services; (b)
          internally use and reproduce the Documentation; (c) grant Authorized
          Users the right to access and use such LazyCloud Services; and (d) use
          the Documentation to assist LazyCloud with the provision of support
          services.
        </p>

        <h3 className="text-lg font-semibold">Access</h3>
        <p>
          Subject to Customer&apos;s payment of the applicable LazyCloud Fees
          and maintenance of a positive usage balance, LazyCloud will provide
          Customer with access to the applicable LazyCloud Services during the
          Service Period. Customer shall use commercially reasonable efforts to
          prevent unauthorized access to, or use of, the LazyCloud Services and
          notify LazyCloud promptly of any such unauthorized use known to
          Customer.
        </p>

        <h3 className="text-lg font-semibold">Authorized Users</h3>
        <p>
          Customer may permit any Authorized Users to access and use the
          features and functions of the LazyCloud Services as contemplated by
          this Agreement.
        </p>

        <h3 className="text-lg font-semibold">Restrictions</h3>
        <p>
          Customer will not, and will not permit any Authorized User or other
          party to: (a) knowingly interfere with or disrupt the integrity or
          performance of the LazyCloud Services or the data contained therein;
          (b) reverse engineer, disassemble or decompile any component of the
          LazyCloud Services; (c) interfere in any manner with the operation of
          the LazyCloud Services or the hardware and network used to operate the
          LazyCloud Services; (d) sublicense any of Customer&apos;s rights under
          this Agreement, or otherwise use the LazyCloud Services for the
          benefit of a third party; (e) modify, copy or make derivative works
          based on any part of the LazyCloud Services; or (f) otherwise use the
          LazyCloud Services in any manner that exceeds the scope of use
          permitted under this Agreement.
        </p>

        <h3 className="text-lg font-semibold">Support</h3>
        <p>
          Subject to the terms of this Agreement, LazyCloud shall use
          commercially reasonable efforts to provide services and support as
          described in the pricing page selected by Customer at
          https://lazycloud.dev/pricing.
        </p>

        <h3 className="text-lg font-semibold">Privacy Policy</h3>
        <p>
          The LazyCloud Services are provided in accordance with our Privacy
          Policy, which can be found at https://lazycloud.dev/legal/privacy.
        </p>
      </div>
    ),
  },
  {
    id: 'section-2',
    title: '2. Ownership',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">LazyCloud Technology</h3>
        <p>
          Customer acknowledges that LazyCloud retains all right, title and
          interest in and to the Documentation and all software and all
          LazyCloud proprietary information and technology used by LazyCloud or
          provided to Customer in connection with the LazyCloud Services (the
          &quot;LazyCloud Technology&quot;), and that the LazyCloud Technology
          is protected by Intellectual Property Rights owned by or licensed to
          LazyCloud. Other than as expressly set forth in this Agreement, no
          license or other rights in the LazyCloud Technology are granted to
          Customer. Customer hereby grants to LazyCloud a royalty-free,
          worldwide, transferable, sublicensable, irrevocable, perpetual license
          to use or incorporate into the LazyCloud Services any suggestions,
          enhancement requests, recommendations or other feedback provided by
          Customer, including Authorized Users, relating to the LazyCloud
          Services. LazyCloud shall not identify Customer as the source of any
          such feedback without Customer&apos;s express prior written consent.
        </p>

        <h3 className="text-lg font-semibold">Customer Data</h3>
        <p>
          The Customer Data hosted by LazyCloud as part of the LazyCloud
          Services, and all worldwide Intellectual Property Rights therein, is
          the exclusive property of Customer. Customer hereby grants to
          LazyCloud a non-exclusive, worldwide, royalty-free and fully paid
          license (a) to use the Customer Data as necessary for purposes of
          providing the LazyCloud Services to Customer and improving the
          LazyCloud Services, and (b) to use the Customer trademarks, service
          marks, and logos as required to provide the LazyCloud Services to
          Customer. All rights in and to the Customer Data not expressly granted
          to LazyCloud in this Agreement are reserved by Customer.
        </p>
      </div>
    ),
  },
  {
    id: 'section-3',
    title: '3. Fees and Expenses; Payments',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">
          Pre-billing and Usage-Based Model
        </h3>
        <p>
          LazyCloud operates on a pre-billing model where customers are charged
          upfront for a usage balance. This balance is then consumed based on
          actual usage of compute resources, storage volumes, and IPv4
          addresses. When the balance is depleted, customer instances will be
          automatically suspended, and access will be restricted until the
          balance is replenished.
        </p>

        <h3 className="text-lg font-semibold">Resource Pricing and Billing</h3>
        <p>
          All fees hereunder are billed in advance for a usage balance. The
          LazyCloud Fees include charges for compute instances, storage volumes,
          and IPv4 addresses. Pricing details are available at
          https://lazycloud.dev/pricing. LazyCloud reserves the right to modify
          pricing with thirty (30) days notice to customers. Any price changes
          will not affect existing usage balances already purchased by
          customers.
        </p>

        <h3 className="text-lg font-semibold">Instance Lifecycle</h3>
        <p>
          All compute instances have a maximum lifespan of 24 hours from
          creation. After 24 hours, instances and their associated resources
          (including volumes and IP addresses) will be automatically destroyed.
          There is no data recovery process available after this 24-hour period.
          Customers are responsible for backing up any important data before the
          24-hour expiration. LazyCloud strongly recommends implementing
          automated backup solutions to prevent data loss.
        </p>

        <h3 className="text-lg font-semibold">Balance Top-up Options</h3>
        <p>
          Customers may replenish their usage balance through manual top-ups or
          by setting up automatic top-ups when the balance falls below a
          specified threshold. Automatic top-ups will be processed according to
          the customer&apos;s configured payment method and threshold settings.
          LazyCloud reserves the right to suspend services if payment fails for
          any reason. Customers are responsible for ensuring their payment
          methods remain valid and have sufficient funds.
        </p>

        <h3 className="text-lg font-semibold">Taxes</h3>
        <p>
          The fees are exclusive of, and Customer will pay, all sales, use,
          excise and other taxes and applicable export and import fees, customs
          duties and similar charges that may be levied upon Customer in
          connection with this Agreement, except for employment taxes for
          LazyCloud employees and taxes based on LazyCloud&apos;s net income.
        </p>

        <h3 className="text-lg font-semibold">Interest</h3>
        <p>
          Any amounts not paid when due shall bear interest at the rate of one
          and one-half percent (1.5%) per month, or the maximum legal rate if
          less.
        </p>
      </div>
    ),
  },
  {
    id: 'section-4',
    title: '4. Customer Content and Responsibilities',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">Customer Warranty</h3>
        <p>
          Customer represents and warrants that any Customer Data hosted by
          LazyCloud as part of the LazyCloud Services shall not (a) infringe,
          misappropriate or violate any Intellectual Property Rights,
          publicity/privacy rights, law or regulation; (b) be deceptive,
          defamatory, obscene, pornographic or unlawful; (c) contain any
          viruses, worms or other malicious computer programming codes intended
          to damage, surreptitiously intercept or expropriate any system, data
          or personal or personally identifiable information; or (d) otherwise
          violate the rights of a third party. LazyCloud is not obligated to
          back up any Customer Data; the Customer is solely responsible for
          creating backup copies of any Customer Data at Customer&apos;s sole
          cost and expense. Customer agrees that any use of the LazyCloud
          Services contrary to or in violation of the representations and
          warranties of Customer in this section constitutes unauthorized and
          improper use of the LazyCloud Services.
        </p>

        <h3 className="text-lg font-semibold">
          Customer Responsibility for Data and Security
        </h3>
        <p>
          Customer and its Authorized Users shall have access to the Customer
          Data and shall be responsible for all changes to and/or deletions of
          Customer Data and the security of all passwords and other access
          protocols required in order the access the LazyCloud Services.
          Customer shall have the ability to export Customer Data out of the
          LazyCloud Services and is encouraged to make its own back-ups of the
          Customer Data. Customer, and not LazyCloud, shall have the sole
          responsibility for the accuracy, quality, integrity, legality,
          reliability, security and appropriateness of all Customer Data.
        </p>

        <h3 className="text-lg font-semibold">Data Retention and Backup</h3>
        <p>
          Customer acknowledges and agrees that LazyCloud does not provide
          long-term data storage or backup services. All data stored on compute
          instances or volumes will be permanently deleted after the 24-hour
          instance lifecycle expires. Customer is solely responsible for
          implementing appropriate data backup and retention strategies.
          LazyCloud recommends that customers regularly export and store their
          data externally to prevent data loss.
        </p>

        <h3 className="text-lg font-semibold">
          Procedure for Making Claims of Intellectual Property Right
          Infringement
        </h3>
        <p>
          It is LazyCloud&apos;s policy to terminate membership privileges of
          any Customer who repeatedly infringes copyright, trademark, or other
          intellectual property rights upon prompt notification to LazyCloud by
          the respective intellectual property owner or their legal agent.
          Without limiting the foregoing, if you believe that your work has been
          copied and posted on the LazyCloud Services in a way that constitutes
          intellectual property rights infringement, please provide our
          designated intellectual property agent with the following information:
          (i) an electronic or physical signature of the person authorized to
          act on behalf of the owner of the copyright, trademark, or other
          intellectual property right; (ii) a description of the copyrighted
          work, trademark, or other intellectual property right that you claim
          has been infringed; (iii) a description of the location on the
          LazyCloud Services of the material that you claim is infringing; (iv)
          your address, telephone number, and email address; (v) a written
          statement by you that you have a good faith belief that the disputed
          use is not authorized by the copyright, trademark, or other
          intellectual property right owner, its agent or the law; and (vi) a
          statement by you, made under penalty of perjury, that the above
          information in your notice is accurate and that you are the copyright,
          trademark, or other intellectual property right owner or authorized to
          act on the copyright, trademark, or other intellectual property right
          owner&apos;s behalf. Contact information for LazyCloud&apos;s
          designated agent for notice of claims of infringement is as follows:
        </p>

        <div className="bg-muted/30 border-lazycloud/50 ml-6 rounded-md border-l-4 p-4">
          <p className="font-medium">DMCA Agent</p>
          <p className="text-muted-foreground">LazyCloud</p>
          <p className="text-muted-foreground">
            <a href="/support" className="text-lazycloud hover:underline">
              LazyCloud Support
            </a>
          </p>
        </div>
      </div>
    ),
  },
  {
    id: 'section-5',
    title: '5. Disclaimer',
    content: (
      <div className="space-y-4">
        <p className="text-sm font-semibold uppercase tracking-wider">
          EXCEPT AS EXPRESSLY PROVIDED IN THIS SECTION AND TO THE MAXIMUM EXTENT
          PERMITTED BY APPLICABLE LAW, THE LAZYCLOUD SERVICES AND DOCUMENTATION
          ARE PROVIDED &quot;AS IS,&quot; &quot;AS AVAILABLE,&quot; AND WITH ALL
          FAULTS, AND LAZYCLOUD AND ITS AFFILIATES, SUPPLIERS, CONTRACTORS, AND
          LICENSORS HEREBY DISCLAIM ALL OTHER WARRANTIES, REPRESENTATIONS, OR
          CONDITIONS, RELATING TO THE LAZYCLOUD SERVICES AND DOCUMENTATION
          WHETHER EXPRESS, IMPLIED OR STATUTORY, INCLUDING, WITHOUT LIMITATION,
          ANY IMPLIED WARRANTIES OF MERCHANTABILITY, TITLE, NONINFRINGEMENT, OR
          FITNESS FOR A PARTICULAR PURPOSE. LAZYCLOUD DOES NOT WARRANT THAT ALL
          ERRORS CAN BE CORRECTED, OR THAT OPERATION OF THE LAZYCLOUD SERVICES
          SHALL BE UNINTERRUPTED, SECURE, OR ERROR-FREE. SOME STATES AND
          JURISDICTIONS DO NOT ALLOW THE EXCLUSION OF IMPLIED WARRANTIES OR
          CONDITIONS OR LIMITATIONS ON HOW LONG AN IMPLIED WARRANTY LASTS, SO
          SOME OF THE ABOVE LIMITATIONS MAY NOT APPLY TO CUSTOMER.
        </p>
      </div>
    ),
  },
  {
    id: 'section-6',
    title: '6. Limitation of Liability',
    content: (
      <div className="space-y-4">
        <p className="text-sm font-semibold uppercase tracking-wider">
          IN NO EVENT WILL LAZYCLOUD OR ITS AFFILIATES, SUPPLIERS, CONTRACTORS,
          OR LICENSORS BE LIABLE FOR ANY SPECIAL, CONSEQUENTIAL, EXEMPLARY,
          INCIDENTAL, OR INDIRECT DAMAGES, INCLUDING LOST PROFITS, IN CONNECTION
          WITH THIS AGREEMENT OR THE LAZYCLOUD SERVICES, EVEN IF PREVIOUSLY
          ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. TO THE MAXIMUM EXTENT
          PERMITTED BY LAW, LAZYCLOUD&apos;S AND ITS AFFILIATES&apos;
          SUPPLIERS&apos; CONTRACTORS&apos; AND LICENSORS&apos; AGGREGATE
          CUMULATIVE LIABILITY UNDER OR RELATING TO THIS AGREEMENT (INCLUDING
          THE LAZYCLOUD SERVICES) WILL NOT EXCEED THE SUM OF ALL AMOUNTS PAID
          AND PAYABLE BY CUSTOMER TO LAZYCLOUD FOR THE USE AND ACCESS TO THE
          LAZYCLOUD SERVICES DURING THE TWELVE (12) MONTHS IMMEDIATELY PRECEDING
          THE EVENT WHICH GAVE RISE TO THE APPLICABLE CLAIM. IT IS EXPRESSLY
          UNDERSTOOD AND AGREED THAT IN THE EVENT ANY REMEDY HEREUNDER IS
          DETERMINED TO HAVE FAILED OF ITS ESSENTIAL PURPOSE, ALL LIMITATIONS OF
          LIABILITY AND EXCLUSIONS OF DAMAGES SET FORTH HEREIN WILL REMAIN IN
          EFFECT.
        </p>
      </div>
    ),
  },
  {
    id: 'section-7',
    title: '7. Confidentiality',
    content: (
      <div className="space-y-4">
        <p>
          &quot;Confidential Information&quot; means any nonpublic information
          of a party (the &quot;Disclosing Party&quot;), whether disclosed
          orally or in written or digital media, that is identified as
          &quot;confidential&quot; or with a similar legend at the time of such
          disclosure or that the receiving party (the &quot;Receiving
          Party&quot;) knows or should have known is the confidential or
          proprietary information of the Disclosing Party. For the avoidance of
          doubt, the LazyCloud Services and Documentation, and all enhancements
          and improvements thereto will be considered Confidential Information
          of LazyCloud. Information will not constitute the other party&apos;s
          Confidential Information if it (i) is already known by the Receiving
          Party without obligation of confidentiality; (ii) is independently
          developed by the Receiving Party without access to or use of the
          Disclosing Party&apos;s Confidential Information; (iii) is publicly
          known without breach of this Agreement; or (iv) is lawfully received
          from a third party without obligation of confidentiality. The
          Receiving Party will not use or disclose any Confidential Information
          except as expressly authorized by this Agreement and will protect the
          Disclosing Party&apos;s Confidential Information using the same degree
          of care that it uses with respect to its own confidential information,
          but in no event with safeguards less than a reasonably prudent
          business would exercise under similar circumstances. The Receiving
          Party will take prompt and appropriate action to prevent unauthorized
          use or disclosure of the Disclosing Party&apos;s Confidential
          Information. Except as subject to applicable law, if any Confidential
          Information must be disclosed to any third party by reason of legal,
          accounting or regulatory requirements, the Receiving Party will
          promptly notify the Disclosing Party of the order or request and
          permit the Disclosing Party (at its own expense) to seek an
          appropriate protective order. Customer acknowledges that LazyCloud can
          collect usage and performance data related to its provision of the
          LazyCloud Services and obtain from third parties Customer&apos;s usage
          data of third-party products and services purchased or acquired
          through LazyCloud (collectively, &quot;Usage Data&quot;). LazyCloud
          may use Usage Data to provide and improve its products and services,
          to market additional products and services to Customer, and disclose
          Usage Data in an aggregated and de-identified manner in connection
          with its business. As between the parties, LazyCloud owns all rights
          in the Usage Data. The parties agree they can have competing products
          and services if these are not developed with or use the other
          party&apos;s Confidential Information.
        </p>
      </div>
    ),
  },
  {
    id: 'section-8',
    title: '8. Indemnification',
    content: (
      <div className="space-y-4">
        <p>
          Customer will defend at its expense any suit brought against
          LazyCloud, and will pay any settlement Customer makes or approves, or
          any damages finally awarded in such suit, insofar as such suit is
          based on a claim arising out of or relating to (a) any use of the
          LazyCloud Services not in accordance with this Agreement or as
          specified in the Documentation; (b) any use of the LazyCloud Services
          in combination with other products, equipment, software or data not
          supplied by LazyCloud; (c) any modification of the LazyCloud Services
          by any person other than LazyCloud or its authorized agents; or (d)
          Customer&apos;s breach or alleged breach of Section 4.1 (Customer
          Warranty).
        </p>
        <p>
          LazyCloud will defend at its expense any suit brought against
          Customer, and will pay any settlement LazyCloud makes or approves, or
          any damages finally awarded in such suit, insofar as such suit is
          based on a claim arising out of or relating to (a) LazyCloud&apos;s
          breach of its confidentiality obligations under Section 7; or (b)
          LazyCloud&apos;s gross negligence or willful misconduct in providing
          the LazyCloud Services. The foregoing indemnification obligation will
          not apply to the extent that the claim is based on Customer&apos;s
          breach of this Agreement or Customer&apos;s use of the LazyCloud
          Services in a manner not authorized by this Agreement.
        </p>
        <p>
          Each party will promptly notify the other party in writing of any
          claim for which the party is seeking indemnification. The indemnifying
          party will have the right to control the defense and settlement of the
          claim, provided that the indemnified party may participate in the
          defense at its own expense. The indemnified party will reasonably
          cooperate with the indemnifying party in the defense and settlement of
          the claim.
        </p>
      </div>
    ),
  },
  {
    id: 'section-9',
    title: '9. Term and Termination',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">Term</h3>
        <p>
          This Agreement commences on the Effective Date and will remain in
          effect until terminated by either party as set forth below.
        </p>

        <h3 className="text-lg font-semibold">Termination</h3>
        <p>
          Either party may terminate this Agreement for no reason or any reason
          upon written notice to the other party, effective immediately. Upon
          termination, any remaining usage balance will be forfeited unless
          otherwise specified in writing by LazyCloud.
        </p>

        <h3 className="text-lg font-semibold">Effect of Termination</h3>
        <p>
          Upon termination or expiration of this Agreement for any reason: (a)
          all rights and obligations of both parties, including all licenses
          granted hereunder, shall immediately terminate; (b) any amounts owed
          to LazyCloud under this Agreement will become immediately due and
          payable; and (c) each party will return to the other all property
          (including any Confidential Information and Customer Data) of the
          other party. The sections and subsections titled Definitions,
          Restrictions, Ownership, Fees and Expenses; Payment, Disclaimer,
          Limitation of Liability, Confidentiality, Indemnification, Effect of
          Termination, and Miscellaneous will survive expiration or termination
          of this Agreement for any reason.
        </p>
      </div>
    ),
  },
  {
    id: 'section-10',
    title: '10. Marketing; Publicity',
    content: (
      <div className="space-y-4">
        <p>
          Customer agrees that LazyCloud may use Customer&apos;s name and logo
          in LazyCloud&apos;s marketing materials or communications (including,
          but not limited to, LazyCloud&apos;s website and in LazyCloud&apos;s
          marketing presentations) for the sole purpose of indicating Customer
          as a user of the LazyCloud Services. Neither party will issue a press
          release announcing its relationship with the other party without the
          other party&apos;s prior approval, not to be unreasonably withheld or
          delayed. Subject to the terms and conditions of this Agreement,
          Customer hereby grants to LazyCloud a non-exclusive and limited
          license to use and publicly display Customer&apos;s logo as set forth
          in this Section.
        </p>
      </div>
    ),
  },
  {
    id: 'section-11',
    title: '11. Miscellaneous',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">Governing Law and Venue</h3>
        <p>
          This Agreement and any action related thereto will be governed and
          interpreted by and under the laws of the State of California, without
          giving effect to any conflicts of laws principles that require the
          application of the law of a different jurisdiction. Customer hereby
          expressly consents to the personal jurisdiction and venue in the state
          and federal courts for the county in which LazyCloud&apos;s principal
          place of business is located for any lawsuit filed there against
          Customer by LazyCloud arising from or related to this Agreement. The
          United Nations Convention on Contracts for the International Sale of
          Goods does not apply to this Agreement.
        </p>

        <h3 className="text-lg font-semibold">Compliance with Law; Export</h3>
        <p>
          Customer agrees to comply with all international and domestic laws,
          ordinances, regulations, and statutes that are applicable to its
          purchase and use of the LazyCloud Services and not export, reexport,
          or transfer, directly or indirectly, any U.S. technical data acquired
          from LazyCloud, or any products utilizing such data, in violation of
          the United States export laws or regulations.
        </p>

        <h3 className="text-lg font-semibold">Severability</h3>
        <p>
          If any provision of this Agreement is, for any reason, held to be
          invalid or unenforceable, the other provisions of this Agreement will
          remain enforceable and the invalid or unenforceable provision will be
          deemed modified so that it is valid and enforceable to the maximum
          extent permitted by law.
        </p>

        <h3 className="text-lg font-semibold">Waiver</h3>
        <p>
          Any waiver or failure to enforce any provision of this Agreement on
          one occasion will not be deemed a waiver of any other provision or of
          such provision on any other occasion.
        </p>

        <h3 className="text-lg font-semibold">No Assignment</h3>
        <p>
          Neither party shall assign, subcontract, delegate, or otherwise
          transfer this Agreement, or its rights and obligations herein, without
          obtaining the prior written consent of the other party, and any
          attempted assignment, subcontract, delegation, or transfer in
          violation of the foregoing will be null and void; provided, however,
          that either party may assign this Agreement in connection with a
          merger, acquisition, reorganization or sale of all or substantially
          all of its assets, or other operation of law, without the consent of
          the other party. The terms of this Agreement shall be binding upon the
          parties and their respective successors and permitted assigns.
        </p>

        <h3 className="text-lg font-semibold">Force Majeure</h3>
        <p>
          LazyCloud will not be liable hereunder by reason of any failure or
          delay in the performance of its obligations under this Agreement on
          account of strikes, shortages, riots, insurrection, fires, flood,
          storm, explosions, acts of God, war, governmental action, labor
          conditions, earthquakes, material shortages or any other cause that is
          beyond the reasonable control of LazyCloud.
        </p>

        <h3 className="text-lg font-semibold">Independent Contractors</h3>
        <p>
          Customer&apos;s relationship to LazyCloud is that of an independent
          contractor, and neither party is an agent or partner of the other.
          Customer will not have, and will not represent to any third party that
          it has, any authority to act on behalf of LazyCloud.
        </p>

        <h3 className="text-lg font-semibold">Notices</h3>
        <p>
          All notices or other communications required or permitted under this
          Agreement will be in writing to the other party. Notices to LazyCloud
          must be sent via{' '}
          <a href="/support" className="text-lazycloud hover:underline">
            LazyCloud Support
          </a>
          . Notices to Customer must be sent to the email address tied to
          Customer&apos;s account. Either party may change its email address for
          receipt of notice by giving notice of such change to the other party.
        </p>

        <h3 className="text-lg font-semibold">Entire Agreement</h3>
        <p>
          This Agreement is the final, complete and exclusive agreement of the
          parties with respect to the subject matters hereof and supersedes and
          merges all prior discussions between the parties with respect to such
          subject matters. No modification of or amendment to this Agreement, or
          any waiver of any rights under this Agreement, will be effective
          unless in writing and signed by an authorized signatory of Customer
          and LazyCloud.
        </p>
      </div>
    ),
  },
  {
    id: 'section-12',
    title: '12. Agreement Updates',
    content: (
      <div className="space-y-4">
        <p>
          When changes are made, LazyCloud will make a new copy of this
          Agreement available on the Services and will also update the
          &quot;Last Updated&quot; date at the top of this Agreement. For any
          material changes, LazyCloud will send Customer an updated copy of this
          Agreement to the email address tied to Customer&apos;s account. Unless
          otherwise stated in such update, any changes to this Agreement will be
          effective immediately for new customers and thirty (30) days after
          posting for existing customers. LazyCloud may require customers to
          provide consent to the updated Agreement in a specified manner before
          further use of the LazyCloud Services. IF CUSTOMER DOES NOT AGREE TO
          ANY CHANGE(S) AFTER RECEIVING A NOTICE OF SUCH CHANGE(S), CUSTOMER
          SHALL STOP USING THE LAZYCLOUD SERVICES.
        </p>
      </div>
    ),
  },
  {
    id: 'section-13',
    title: '13. Definitions',
    content: (
      <div className="space-y-4">
        <p>As used in this Agreement:</p>
        <ul className="list-disc space-y-2 pl-6">
          <li>
            <span className="font-medium">Authorized User</span> means each of
            Customer&apos;s employees, agents, and independent contractors who
            create or are provided usernames and passwords and permitted
            hereunder to access the LazyCloud Services pursuant to
            Customer&apos;s rights under this Agreement.
          </li>
          <li>
            <span className="font-medium">Documentation</span> means the
            technical materials provided or made available by LazyCloud to
            Customer in hard copy or electronic form that describe the features,
            functionality or operation of the LazyCloud Services.
          </li>
          <li>
            <span className="font-medium">Effective Date</span> means the date
            on which Customer first began using the LazyCloud Services.
          </li>
          <li>
            <span className="font-medium">LazyCloud Fees</span> means the then
            current fees for the LazyCloud Services set forth at
            https://lazycloud.dev/pricing, including charges for compute
            instances, storage volumes, and IPv4 addresses.
          </li>
          <li>
            <span className="font-medium">Usage Balance</span> means the
            pre-paid amount available for consumption of LazyCloud Services,
            which is depleted based on actual resource usage.
          </li>
          <li>
            <span className="font-medium">Instance Lifecycle</span> means the
            24-hour period from creation during which a compute instance and its
            associated resources remain active before automatic destruction.
          </li>
          <li>
            <span className="font-medium">Intellectual Property Rights</span>{' '}
            means any and all now known or hereafter existing (a) rights
            associated with works of authorship, including copyrights, mask work
            rights, and moral rights; (b) trademark or service mark rights; (c)
            trade secret rights; (d) patents, patent rights, and industrial
            property rights; (e) layout design rights, design rights, and other
            proprietary rights of every kind and nature other than trademarks,
            service marks, trade dress, and similar rights; and (f) all
            registrations, applications, renewals, extensions, or reissues of
            the foregoing, in each case in any jurisdiction throughout the
            world.
          </li>
          <li>
            <span className="font-medium">Customer Data</span> means any data
            and/or other content provided or developed by or on behalf of
            Customer and used with the LazyCloud Services.
          </li>
          <li>
            <span className="font-medium">Service Period</span> means the period
            during which Customer has access to the LazyCloud Services, which
            continues until terminated in accordance with Section 9.2.
          </li>
        </ul>
      </div>
    ),
  },
  {
    id: 'section-14',
    title: '14. Support',
    content: (
      <div className="space-y-4">
        <p>
          If you have any questions about these Terms or if you wish to make any
          complaint or claim with respect to the Services, please{' '}
          <a href="/support" className="text-lazycloud hover:underline">
            contact support
          </a>
          .
        </p>
        <p>
          When submitting a complaint, please provide a brief description of the
          nature of your complaint and the specific services to which your
          complaint relates.
        </p>
      </div>
    ),
  },
  {
    id: 'section-15',
    title: '15. Service Availability and Support',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">Service Availability</h3>
        <p>
          LazyCloud strives to provide high availability for its services but
          does not guarantee uninterrupted, error-free, or secure access to the
          LazyCloud Services. LazyCloud may perform scheduled maintenance with
          notice to customers and unscheduled maintenance as needed. During
          maintenance periods, some or all of the LazyCloud Services may be
          unavailable. LazyCloud will use commercially reasonable efforts to
          minimize service disruptions.
        </p>

        <h3 className="text-lg font-semibold">Service Level Agreement</h3>
        <p>
          LazyCloud&apos;s service level agreement (SLA) is available at
          https://lazycloud.dev/legal/sla. The SLA sets forth LazyCloud&apos;s
          commitment to service availability and the remedies available to
          customers if LazyCloud fails to meet those commitments. The SLA is
          incorporated by reference into this Agreement.
        </p>

        <h3 className="text-lg font-semibold">Support Services</h3>
        <p>
          LazyCloud provides support services in accordance with the support
          plan selected by Customer. Support details are available at
          https://lazycloud.dev/support. LazyCloud may modify its support
          services from time to time, provided that any material reduction in
          support services will not apply to existing customers until the end of
          their current Service Period.
        </p>
      </div>
    ),
  },
  {
    id: 'section-16',
    title: '16. Dispute Resolution',
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">Informal Resolution</h3>
        <p>
          Before initiating any formal dispute resolution process, the parties
          agree to attempt to resolve any dispute, claim, or controversy arising
          out of or relating to this Agreement through good-faith negotiation.
          The party seeking to initiate formal dispute resolution must first
          send a written notice to the other party describing the nature of the
          dispute and the relief sought. The parties will then attempt to
          resolve the dispute through negotiation within thirty (30) days of the
          notice.
        </p>

        <h3 className="text-lg font-semibold">Arbitration</h3>
        <p>
          If the parties are unable to resolve the dispute through negotiation,
          any dispute, claim, or controversy arising out of or relating to this
          Agreement will be resolved through binding arbitration in accordance
          with the American Arbitration Association&apos;s Commercial
          Arbitration Rules. The arbitration will be conducted in English in San
          Francisco, California. The arbitration will be conducted by a single
          arbitrator appointed by the American Arbitration Association. The
          arbitrator&apos;s decision will be final and binding on both parties.
        </p>

        <h3 className="text-lg font-semibold">Class Action Waiver</h3>
        <p>
          The parties agree that any arbitration will be conducted on an
          individual basis, and neither party may bring a claim as part of a
          class, consolidated, or representative action. The arbitrator may not
          consolidate more than one person&apos;s claims and may not otherwise
          preside over any form of a class, consolidated, or representative
          proceeding.
        </p>

        <h3 className="text-lg font-semibold">Small Claims Court</h3>
        <p>
          Notwithstanding the foregoing, either party may bring a claim in small
          claims court if the claim is for less than $10,000 and is not part of
          a class, consolidated, or representative action.
        </p>
      </div>
    ),
  },
]

function TermsPage() {
  const [expandAll, setExpandAll] = useState(false)
  const effectiveDate = new Date().toLocaleDateString('en-US', {
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  })

  const toggleAll = () => {
    setExpandAll(!expandAll)
  }

  return (
    <main className="flex min-h-screen flex-1 overflow-hidden px-4 pt-20 pb-8 sm:px-6 sm:pt-24 lg:px-8">
      <div className="bg-card border-border/50 mx-auto flex w-full max-w-5xl flex-1 flex-col overflow-hidden rounded-xl border p-6 shadow-sm sm:p-8 lg:p-10">
        <div className="mb-12 text-center">
          <h1 className="mb-3 text-3xl font-bold tracking-tight sm:text-4xl md:text-5xl">
            Terms of Service
          </h1>
          <p className="text-muted-foreground text-sm">
            Effective date: {effectiveDate}
          </p>
        </div>

        <Card className="border-border/50 mb-8 shadow-sm">
          <CardContent className="p-6">
            <div className="flex flex-col items-start gap-6 md:flex-row md:items-center">
              <div className="bg-muted flex-shrink-0 rounded-full p-3">
                <AlertCircle className="size-6 text-amber-500" />
              </div>
              <div className="flex-1 space-y-2">
                <h2 className="text-lg font-semibold">Important Notice</h2>
                <p className="text-muted-foreground text-sm">
                  By using LazyCloud, you agree to these terms. Please read them
                  carefully. Usage balance is consumed based on actual resource
                  usage and must be replenished when depleted.
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
                      <ChevronUp className="size-4" />
                      <span>Collapse All</span>
                    </>
                  ) : (
                    <>
                      <ChevronDown className="size-4" />
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
            Please read this Terms of Service agreement carefully before
            accessing or using LazyCloud.
          </p>
          <p className="text-muted-foreground">
            Welcome and thank you for your interest in LazyCloud
            (&quot;LazyCloud&quot;). These Terms of Service (this
            &quot;Agreement&quot;) describes the terms and conditions that apply
            to your use of our website located at lazycloud.dev and its
            subdomains (collectively, the &quot;Website&quot;) and the products,
            services, content and other resources available on or enabled via
            our Website (collectively, the &quot;LazyCloud Services&quot;).
          </p>
        </div>

        <div className="mb-12 grid grid-cols-1 gap-6 md:grid-cols-3">
          <FeatureCard
            icon={<Shield className="size-6 text-emerald-500" />}
            title="Data Protection"
            description="We take your data security seriously and implement industry-standard protections."
          />
          <FeatureCard
            icon={<Clock className="size-6 text-blue-500" />}
            title="Pre-billing Model"
            description="Pay upfront for a usage balance that is consumed based on actual resource usage. Top off manually or automatically."
          />
          <FeatureCard
            icon={<FileText className="size-6 text-purple-500" />}
            title="Documentation"
            description="Access to comprehensive documentation to help you get the most from our services."
          />
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
            If you have any questions about these Terms or if you wish to make
            any complaint or claim with respect to the Services, please{' '}
            <a
              href="/support"
              className="text-lazycloud font-medium hover:underline"
            >
              contact support
            </a>
            .
          </p>
          <p className="text-muted-foreground mt-4 text-sm">
            When submitting a complaint, please provide a brief description of
            the nature of your complaint and the specific services to which your
            complaint relates.
          </p>
        </div>

        <div className="text-muted-foreground text-center text-sm">
          <p>
            &copy; {new Date().getFullYear()} LazyCloud. All rights reserved.
          </p>
        </div>
      </div>
    </main>
  )
}
