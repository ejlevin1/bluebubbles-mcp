# CHANGELOG

<!-- version list -->

## v1.0.0 (2026-08-31)

### Bug Fixes

- **ci**: Move actions off the deprecated Node 20 runtime
  ([`64769d3`](https://github.com/ejlevin1/bluebubbles-mcp/commit/64769d3dae133fb6d2e114b602c374b28390aa5d))

- **ci**: Push the release commit with a PAT
  ([`45bc6d0`](https://github.com/ejlevin1/bluebubbles-mcp/commit/45bc6d0bb4f83745b10541cc450a20b2dd926f4e))

### Features

- **send**: Use the Private API when the server supports it
  ([`ecaadb6`](https://github.com/ejlevin1/bluebubbles-mcp/commit/ecaadb6f6442fea8a9744de159ce83a342403ac5))

- **server**: Add get_my_address identity enrichment
  ([`5b56fa5`](https://github.com/ejlevin1/bluebubbles-mcp/commit/5b56fa5d78ce5549c9138af16ba113fbe19586f4))

### Breaking Changes

- **send**: `method` is removed from BlueBubblesClient.send_message, send_message_to_address,
  send_attachment and create_scheduled_message. The send method is now a property of the connection,
  set from /server/info or BLUEBUBBLES_SEND_METHOD. The MCP tool surface is unaffected — no tool
  call ever passed `method`. `send_message_to_address`'s `service` is now a strict
  `Literal["iMessage", "SMS"]`, so values outside the enum are refused instead of silently sending
  over iMessage.


## v0.5.2 (2026-08-30)

### Bug Fixes

- **ci**: Build the versioned image after the tag exists
  ([`f8c74ed`](https://github.com/ejlevin1/bluebubbles-mcp/commit/f8c74ed9bc5fbae3ce3e0f41b63b364eedd97d7f))


## v0.5.1 (2026-08-30)

### Bug Fixes

- **ci**: Drop the build command that broke the release job
  ([`0545dad`](https://github.com/ejlevin1/bluebubbles-mcp/commit/0545dad14110e91cfc70305dcad5a7b801341240))


## v0.5.0 (2026-08-30)

- Initial Release
