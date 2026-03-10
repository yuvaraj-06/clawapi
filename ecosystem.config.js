module.exports = {
  apps: [
    {
      name: "clawapi",
      script: "/home/ubuntu/clawapi/venv/bin/uvicorn",
      args: "main:app --host 127.0.0.1 --port 8100 --workers 4",
      cwd: "/home/ubuntu/clawapi",
      interpreter: "none",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 3000,
      env: {
        NODE_ENV: "production",
        ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY || ""
      },
      error_file: "/home/ubuntu/clawapi/logs/pm2-error.log",
      out_file: "/home/ubuntu/clawapi/logs/pm2-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss"
    }
  ]
}
