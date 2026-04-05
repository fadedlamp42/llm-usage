// pm2 ecosystem config for llm-usage
// usage: pm2 start ecosystem.config.cjs

module.exports = {
  apps: [
    {
      name: "llm-usage",
      script: "start.sh",
      cwd: __dirname,
      interpreter: "bash",

      autorestart: true,
      max_restarts: 10,
      min_uptime: "10s",
      restart_delay: 5000,

      watch: false,

      // logging
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      merge_logs: true,

      // graceful shutdown — SIGTERM lets it restore brightness
      kill_timeout: 5000,
    },
  ],
};
