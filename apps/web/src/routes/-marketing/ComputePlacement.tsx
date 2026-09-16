import "./computePlacement.css";

export const computeDestinations = [
  {
    key: "managed",
    slot: 5,
    title: "LazyCloud",
    body: "Deploy without managing servers. LazyCloud provisions CPU capacity and scales down when idle.",
  },
  {
    key: "aws",
    slot: 7,
    title: "Your AWS account",
    body: "Use CPU and GPU capacity in your AWS account. Manage deployments through LazyCloud.",
  },
  {
    key: "machines",
    slot: 9,
    title: "Your own machines",
    body: "Connect your Linux servers, VMs, or GPU machines. Deploy through the same Python API.",
  },
] as const;

const stackSlots = Array.from({ length: 39 }, (_, index) => index - 12);

function ComputeExample({
  destination,
}: {
  destination: (typeof computeDestinations)[number]["key"];
}) {
  if (destination === "managed") {
    return (
      <div className="compute-example">
        <div className="compute-example-heading">
          <strong>support-agent</strong>
          <span>Managed CPU</span>
        </div>
        <div className="compute-traffic">
          <span>Incoming requests</span>
          <svg viewBox="0 0 520 96" fill="none" aria-hidden="true">
            <path className="compute-chart-grid" d="M0 24H520M0 56H520M0 88H520" />
            <path
              className="compute-chart-fill"
              d="M0 80L32 79L58 64L82 72L110 54L136 61L168 28L192 40L220 18L250 36L276 32L302 56L330 44L358 65L392 59L420 77L452 72L484 83L520 82V96H0Z"
            />
            <path
              className="compute-chart-line"
              d="M0 80L32 79L58 64L82 72L110 54L136 61L168 28L192 40L220 18L250 36L276 32L302 56L330 44L358 65L392 59L420 77L452 72L484 83L520 82"
            />
          </svg>
        </div>
        <div className="compute-service">
          <span>/chat</span>
          <span>API</span>
          <span className="compute-service-track">
            <i />
            <i />
            <i />
            <i />
            <i />
          </span>
        </div>
        <div className="compute-service">
          <span>summarize</span>
          <span>Function</span>
          <span className="compute-service-track">
            <i />
            <i />
            <i />
          </span>
        </div>
        <div className="compute-example-footer">
          <span>Capacity follows demand</span>
          <span>Scale to zero</span>
        </div>
      </div>
    );
  }
  if (destination === "aws") {
    return (
      <div className="compute-example">
        <div className="compute-example-heading">
          <strong>document-search</strong>
          <span>Your AWS account</span>
        </div>
        <div className="compute-account">
          <div className="compute-account-heading">
            <span>Compute capacity</span>
            <span>CPU + GPU</span>
          </div>
          <div className="compute-capacity-pool">
            <div className="compute-gpu">
              <span>GPU</span>
              <strong>embed</strong>
              <div className="compute-core-grid" aria-hidden="true">
                {Array.from({ length: 24 }, (_, i) => (
                  <i key={i} />
                ))}
              </div>
            </div>
            <div className="compute-cpu">
              <span>CPU</span>
              <strong>search-api</strong>
              <div className="compute-core-grid" aria-hidden="true">
                {Array.from({ length: 12 }, (_, i) => (
                  <i key={i} />
                ))}
              </div>
            </div>
          </div>
        </div>
        <div className="compute-example-footer">
          <span>Deploy through LazyCloud</span>
          <span>Run in your account</span>
        </div>
      </div>
    );
  }
  return (
    <div className="compute-example">
      <div className="compute-example-heading">
        <strong>research-agent</strong>
        <span>Your machines</span>
      </div>
      <div className="compute-fleet">
        {[
          { name: "gpu-01", kind: "GPU", job: "inference", blocks: 7 },
          { name: "worker-02", kind: "CPU", job: "process-documents", blocks: 5 },
          { name: "server-03", kind: "CPU", job: "agent-api", blocks: 3 },
        ].map((machine) => (
          <div className="compute-machine" key={machine.name}>
            <div className="compute-machine-heading">
              <strong>{machine.name}</strong>
              <span>{machine.kind}</span>
            </div>
            <div className="compute-machine-allocation">
              <span>{machine.job}</span>
              <div aria-hidden="true">
                {Array.from({ length: 8 }, (_, i) => (
                  <i key={i} data-filled={i < machine.blocks} />
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="compute-example-footer">
        <span>Connected Linux machines</span>
        <span>One deployment API</span>
      </div>
    </div>
  );
}

export function ComputePlacement({
  selectedIndex,
  isDesktop,
}: {
  selectedIndex: number;
  isDesktop: boolean;
}) {
  return (
    <div className="compute-scene" data-destination={computeDestinations[selectedIndex].key}>
      <div className="compute-scene-atmosphere" aria-hidden="true" />
      {stackSlots.map(
        (index) =>
          !computeDestinations.some((example) => example.slot === index) && (
            <div
              className="compute-stack-card"
              aria-hidden="true"
              key={index}
              style={{
                top: `calc(50% + ${index - 7} * var(--compute-stack-step))`,
                zIndex: 39 - index,
              }}
            >
              <div className="compute-deployment compute-empty-slot">
                <div className="compute-material" />
              </div>
            </div>
          ),
      )}
      {computeDestinations.map((example, index) => (
        <div
          className="compute-stage"
          key={example.key}
          data-active={index === selectedIndex}
          style={{
            top: `calc(50% + ${example.slot - 7} * var(--compute-stack-step))`,
            zIndex: 39 - example.slot,
          }}
          aria-hidden={isDesktop && index !== selectedIndex}
        >
          <figure className="compute-deployment" aria-label={`${example.title} deployment example`}>
            <div className="compute-material" aria-hidden="true" />
            <div className="compute-deployment-content">
              <div className="compute-mobile-description">
                <h3>{example.title}</h3>
                <p>{example.body}</p>
              </div>
              <ComputeExample destination={example.key} />
            </div>
          </figure>
        </div>
      ))}
    </div>
  );
}
