import { defineRailway, github, preserve, project, service } from "railway/iac";

// Populate private variables from deploy/railway/*.env.example in Railway first.
// preserve() leaves those secrets/settings in Railway, never in this repository.
const backendEnvironment = {
  APP_ENV: "production",
  DATABASE_URL: preserve(),
  REDIS_URL: preserve(),
  BOT_API_KEY: preserve(),
  BOT_IDENTITY: "omni-task-production",
  BOT_BASE_URL: "http://${{bot.RAILWAY_PRIVATE_DOMAIN}}:8001",
  DASHBOARD_URL: "https://neytrib.github.io/omni-task/",
  DASHBOARD_ORIGIN: "https://neytrib.github.io",
  COOKIE_NAME: "__Host-omni_session",
  COOKIE_SECURE: "true",
  COOKIE_SAMESITE: "none",
  COOKIE_PARTITIONED: "true",
  CELERY_BROKER_KEY_PREFIX: "omni-task:production:",
  LIVE_CHANNEL: "omni-task:production:live:v1",
  TRANSCRIPTION_PROVIDER: preserve(),
  TRANSCRIPTION_MODEL: preserve(),
} as const;

const deployment = {
  restartPolicyType: "ON_FAILURE",
  restartPolicyMaxRetries: 5,
  sleepApplication: false,
  overlapSeconds: 0,
  drainingSeconds: 30,
} as const;

const backendBuild = {
  builder: "DOCKERFILE",
  dockerfilePath: "deploy/railway/backend.Dockerfile",
  watchPatterns: ["/backend/**", "/scripts/**", "/deploy/railway/**", "/pyproject.toml", "/uv.lock", "/omni_logging.py", "/alembic.ini", "/.dockerignore"],
} as const;

export default defineRailway(() => {
  const api = service("api", {
    source: github("Neytrib/omni-task", { branch: "main", rootDirectory: "/" }),
    build: { ...backendBuild, watchPatterns: [...backendBuild.watchPatterns] },
    start: "python deploy/railway/serve.py api",
    healthcheck: "/api/health/ready",
    healthcheckTimeout: 180,
    replicas: 1,
    deploy: deployment,
    env: { ...backendEnvironment, PORT: "8000", ALLOW_PAID_TRANSCRIPTION: preserve() },
  });

  // Intentionally no GitHub source. A serialized GitHub Actions workflow stops
  // the previous deployment before uploading a replacement. Railway rolling
  // deploys can otherwise start two Telegram pollers even with one replica.
  const bot = service("bot", {
    build: { builder: "DOCKERFILE", dockerfilePath: "deploy/railway/bot.Dockerfile" },
    start: "python deploy/railway/serve.py bot",
    healthcheck: "/health/ready",
    healthcheckTimeout: 180,
    replicas: 1,
    deploy: deployment,
    env: {
      PORT: "8001",
      API_BASE_URL: "http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000",
      BOT_API_KEY: api.env.BOT_API_KEY,
      TELEGRAM_BOT_TOKEN: preserve(),
    },
  });

  const worker = service("worker", {
    source: github("Neytrib/omni-task", { branch: "main", rootDirectory: "/" }),
    build: { ...backendBuild, watchPatterns: [...backendBuild.watchPatterns] },
    start: "python -m app.jobs.worker",
    replicas: 1,
    deploy: { ...deployment, drainingSeconds: 35 },
    env: {
      ...backendEnvironment,
      DATABASE_URL: api.env.DATABASE_URL,
      REDIS_URL: api.env.REDIS_URL,
      BOT_API_KEY: api.env.BOT_API_KEY,
      OPENAI_API_KEY: preserve(),
      ALLOW_PAID_TRANSCRIPTION: preserve(),
    },
  });

  // Neon and Upstash are externally managed; do not provision duplicate databases.
  // Migrations are an explicit release operation, never a service start/preDeploy hook.
  return project("omni-task", { resources: [api, bot, worker] });
});
