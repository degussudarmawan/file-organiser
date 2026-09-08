"""Entry point for the MCP server, mirroring run.py for the web server.

`organiser/mcp_server.py` uses relative imports, so loading it *as a file*
fails with "attempted relative import with no known parent package" — and a
lot of MCP tooling (the Inspector, `fastmcp list`, most client configs) wants
a file path rather than a module. This is that file path.

    python serve_mcp.py                         run it directly
    fastmcp list serve_mcp.py                   what tools does it expose
    fastmcp dev inspector serve_mcp.py          poke at it in the Inspector

Everything real lives in organiser/mcp_server.py; this only fixes how it is
loaded.
"""

from organiser.mcp_server import main, mcp  # noqa: F401  (mcp: for tooling)

if __name__ == "__main__":
    main()
