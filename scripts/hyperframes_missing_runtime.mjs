#!/usr/bin/env node
console.error(
  "OFFLINE_HYPERFRAMES_RUNTIME_MISSING: bundled HyperFrames CLI is not installed. " +
  "Run deploy/setup to install the offline runtime before rendering."
);
process.exit(127);
