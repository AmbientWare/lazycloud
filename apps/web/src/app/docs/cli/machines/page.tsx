"use client";

import RemoteMarkdown from "../../_components/RemoteMarkdown";

export default function MachinesPage() {
  return (
    <div className="container mx-auto px-4 py-8">
      <RemoteMarkdown path="cli/machines.mdx" />
    </div>
  );
}
