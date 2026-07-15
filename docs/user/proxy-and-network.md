# Proxy And Network Target

> Pre-release target: this configuration is not yet validated in a packaged
> Desktop release.

Remote setup needs to support:

- HTTP and HTTPS proxy URLs without user information;
- a no-proxy host list;
- pip index URL;
- Hugging Face endpoint and cache location;
- SSH jump/proxy configuration where supported.

Settings apply to OpenEvo-managed remote downloads and services, not only the
local Desktop process. They do not bypass institutional policy. After retries,
Desktop must report the failing phase and the remaining user action without
logging proxy credentials.

Authenticated proxy slots are reserved and unavailable in the exhibition
candidate. A proxy URL containing user information is rejected rather than
downgraded to an unauthenticated request.
