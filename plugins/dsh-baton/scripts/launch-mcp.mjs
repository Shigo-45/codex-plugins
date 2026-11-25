#!/usr/bin/env node
// Portable MCP entry: fail with the supported runtime floor before loading the
// vendored dsh-baton server. Keep protocol stdout clear for MCP frames.
const MIN = [22, 15]
const [major, minor] = process.versions.node.split('.').map(Number)
if (major < MIN[0] || (major === MIN[0] && minor < MIN[1])) {
  console.error(`dsh-baton requires Node.js >=${MIN.join('.')}; found ${process.versions.node}.`)
  process.exit(1)
}
await import('../app/src/server.mjs')
