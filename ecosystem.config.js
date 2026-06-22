/** PM2 process definitions for Local Google MCP. */
module.exports = {
  apps: [
    {
      name: "local-google-mcp",
      script: "scripts/start.sh",
      interpreter: "bash",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
