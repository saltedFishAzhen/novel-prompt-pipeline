# Security reporting

Please do not include API keys, private source texts, generated media, or
exploitable details in a public issue. Use the repository host's private
vulnerability-reporting feature if the maintainers have enabled it, or use a
private contact channel explicitly published by the maintainers.

No private security contact is published in this repository yet. Maintainers
should establish one before opening the project to public vulnerability
reports. Do not assume a public issue is private.

The MCP server uses stdio for local integration. Treat a configured MCP client
and its workspace as trusted: the exposed tools can read project data, update
assets, publish prompts, and start jobs. A cancelled client request does not
necessarily cancel a model request or an already submitted ComfyUI task.

Store credentials in environment variables or an untracked `.env` file. Run
outputs can contain complete input text and model responses; protect them and
the asset-library files according to your own retention and access needs.
