import { ImageResponse } from "next/og";

export const runtime = "edge";

export const alt = "LazyCloud - Deploy Docker Compose to the Cloud";
export const size = {
  width: 1200,
  height: 630,
};
export const contentType = "image/png";

export default async function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          height: "100%",
          width: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: "#0a0a0a",
          backgroundImage:
            "radial-gradient(circle at 25% 25%, #1a1a2e 0%, transparent 50%), radial-gradient(circle at 75% 75%, #16213e 0%, transparent 50%)",
        }}
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              fontSize: 72,
              fontWeight: 700,
              color: "#ffffff",
              marginBottom: 20,
              display: "flex",
              alignItems: "center",
              gap: 20,
            }}
          >
            <span style={{ fontSize: 80 }}>☁️</span>
            <span>LazyCloud</span>
          </div>
          <div
            style={{
              fontSize: 32,
              color: "#a0a0a0",
              textAlign: "center",
              maxWidth: 800,
              lineHeight: 1.4,
            }}
          >
            Deploy Docker Compose to the Cloud in Seconds
          </div>
          <div
            style={{
              marginTop: 40,
              padding: "16px 32px",
              backgroundColor: "#3b82f6",
              borderRadius: 12,
              fontSize: 24,
              color: "#ffffff",
              fontWeight: 600,
            }}
          >
            No Kubernetes. No Infrastructure. Just Deploy.
          </div>
        </div>
      </div>
    ),
    {
      ...size,
    }
  );
}
