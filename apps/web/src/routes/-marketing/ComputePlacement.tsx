import { computeDestinations } from "./computeDestinations";
import "./computePlacement.css";

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
          <strong>coding-agent</strong>
          <span>Sandbox</span>
        </div>
        <div className="compute-agent-task">
          <span>Task</span>
          <strong>Add search to the API</strong>
        </div>
        <div className="compute-workspace">
          <div className="compute-workspace-files">
            <span>Workspace</span>
            <div>
              app/
              <div>
                api.py <em>+8</em>
              </div>
              <div>
                search.py <em>+32</em>
              </div>
            </div>
            <div>
              tests/
              <div>
                test_search.py <em>+24</em>
              </div>
            </div>
          </div>
          <div className="compute-agent-actions">
            <div>
              <span>Read files</span>
              <small>app/api.py</small>
            </div>
            <div>
              <span>Edit source</span>
              <small>app/search.py</small>
            </div>
            <div data-current="true">
              <span>Run tests</span>
              <small>uv run pytest</small>
            </div>
          </div>
        </div>
        <div className="compute-sandbox-terminal">
          <span>$ uv run pytest</span>
          <div>
            tests/test_search.py <span>···</span>
            <i aria-hidden="true" />
          </div>
        </div>
      </div>
    );
  }
  if (destination === "aws") {
    return (
      <div className="compute-example">
        <div className="compute-example-heading">
          <strong>product-api</strong>
          <span>Endpoints</span>
        </div>
        <div className="compute-route-heading">
          <span>Your AWS account</span>
          <span>Request activity</span>
        </div>
        <div className="compute-endpoints">
          {[
            {
              method: "POST",
              path: "/search",
              trace: "M0 25H18L24 12L30 29L37 20L44 25H66L72 7L79 29L86 19L93 25H120",
            },
            {
              method: "POST",
              path: "/chat",
              trace: "M0 25H12L19 18L25 28L33 25H47L53 8L60 30L68 16L75 25H98L104 20L111 25H120",
            },
            {
              method: "GET",
              path: "/documents",
              trace: "M0 25H24L30 17L36 28L42 25H74L80 13L87 29L94 21L101 25H120",
            },
          ].map((route) => (
            <div className="compute-endpoint" key={route.path}>
              <span className="compute-http-method">{route.method}</span>
              <strong>{route.path}</strong>
              <svg viewBox="0 0 120 36" fill="none" aria-hidden="true">
                <path className="compute-chart-line" d={route.trace} />
              </svg>
              <span className="compute-http-status">200</span>
            </div>
          ))}
        </div>
        <div className="compute-route-footer">
          <span>HTTPS requests</span>
          <div aria-hidden="true">
            <i />
            <i />
            <i />
            <i />
            <i />
            <i />
            <i />
            <i />
            <i />
            <i />
            <i />
            <i />
          </div>
          <span>Responses</span>
        </div>
      </div>
    );
  }
  return (
    <div className="compute-example">
      <div className="compute-example-heading">
        <strong>model-training</strong>
        <span>gpu-server-01</span>
      </div>
      <div className="compute-training-summary">
        <div>
          <span>Training loss</span>
          <strong>0.24</strong>
        </div>
        <div>
          <span>Epoch</span>
          <strong>
            06 <small>/ 10</small>
          </strong>
        </div>
      </div>
      <div className="compute-training-chart">
        <svg
          viewBox="0 0 520 130"
          fill="none"
          role="img"
          aria-label="Training loss decreases as the run progresses"
        >
          <path className="compute-chart-grid" d="M0 12H520M0 48H520M0 84H520M0 120H520" />
          <path
            className="compute-chart-fill"
            d="M0 8L18 32L34 23L52 46L68 40L86 64L104 55L122 75L140 68L160 84L182 79L204 93L225 88L248 99L270 94L294 104L320 101L346 110L370 105L396 113L422 110L448 117L476 115L502 120L520 118V130H0Z"
          />
          <path
            className="compute-chart-line"
            d="M0 8L18 32L34 23L52 46L68 40L86 64L104 55L122 75L140 68L160 84L182 79L204 93L225 88L248 99L270 94L294 104L320 101L346 110L370 105L396 113L422 110L448 117L476 115L502 120L520 118"
          />
        </svg>
        <div>
          <span>0</span>
          <span>Training steps</span>
          <span>6,000</span>
        </div>
      </div>
      <div className="compute-checkpoint">
        <span>Checkpoint</span>
        <strong>epoch-05.safetensors</strong>
      </div>
      <div className="compute-training-progress" aria-hidden="true">
        <i />
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
