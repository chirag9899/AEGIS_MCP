/** PM2 process definitions for Aegis Google MCP. */
module.exports = {
  apps: [
    {
      name: "aegis-google-mcp",
      script: "scripts/start.sh",
      interpreter: "bash",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
