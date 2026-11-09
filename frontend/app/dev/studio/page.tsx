"use client";

import dynamic from "next/dynamic";

// Studio reads browser layout while mounting.
const Probe = dynamic(() => import("../../../studio/probe"), { ssr: false });

export default function StudioProbePage() {
  return (
    <section aria-labelledby="studio-title" style={{ padding: "1rem" }}>
      <h1 id="studio-title" style={{ fontSize: "1.7rem" }}>Studio integration</h1>
      <p>Inspect the frontend package manifest with the local dashboard agent.</p>
      <Probe />
    </section>
  );
}
