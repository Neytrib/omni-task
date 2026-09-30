import assert from "node:assert/strict";
import test from "node:test";
import { createRailwayContext, project } from "railway/iac";
import configuration from "./railway.ts";

const graph = await configuration(createRailwayContext({ environment: "production" }), project);
const services = Object.fromEntries(graph.resources.map((resource) => [resource.name, resource]));

test("one repository, exactly three services, and no duplicate hosted databases", () => {
  assert.deepEqual(Object.keys(services), ["api", "bot", "worker"]);
  for (const name of ["api", "worker"]) {
    assert.equal(services[name].source.repo, "Neytrib/omni-task");
    assert.equal(services[name].source.branch, "main");
    assert.equal(services[name].source.rootDirectory, "/");
  }
  assert.equal(services.bot.source, undefined);
});

test("deployment bounds, health checks and migration separation survive SDK normalization", () => {
  for (const service of Object.values(services)) {
    assert.equal(service.deploy.numReplicas, 1);
    assert.equal(service.deploy.restartPolicyType, "ON_FAILURE");
    assert.equal(service.deploy.restartPolicyMaxRetries, 5);
    assert.equal(service.deploy.sleepApplication, false);
    assert.equal(service.deploy.overlapSeconds, 0);
    assert.equal(service.deploy.preDeployCommand, undefined);
    assert.doesNotMatch(service.deploy.startCommand, /alembic|migrate/);
  }
  assert.equal(services.api.deploy.healthcheckPath, "/api/health/ready");
  assert.equal(services.bot.deploy.healthcheckPath, "/health/ready");
  assert.equal(services.worker.deploy.startCommand, "python -m app.jobs.worker");
});

test("bot has no persistence/provider secrets and private URLs contain only references", () => {
  assert.deepEqual(Object.keys(services.bot.variables).sort(), ["API_BASE_URL", "BOT_API_KEY", "PORT", "TELEGRAM_BOT_TOKEN"]);
  assert.equal(services.bot.variables.TELEGRAM_BOT_TOKEN.type, "preserve");
  assert.equal(services.api.variables.DATABASE_URL.type, "preserve");
  assert.equal(services.api.variables.REDIS_URL.type, "preserve");
  assert.equal(services.api.variables.ALLOW_PAID_TRANSCRIPTION.type, "preserve");
  assert.equal(services.worker.variables.OPENAI_API_KEY.type, "preserve");
  assert.equal(services.worker.variables.ALLOW_PAID_TRANSCRIPTION.type, "preserve");
  assert.equal(services.bot.variables.BOT_API_KEY.type, "reference");
  assert.equal(services.bot.variables.API_BASE_URL.value, "http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000");
  for (const service of Object.values(services)) {
    assert.equal(service.networking, undefined);
  }
});

test("Railway builds explicit production images rather than the local Dockerfile test target", () => {
  assert.equal(services.api.build.dockerfilePath, "deploy/railway/backend.Dockerfile");
  assert.equal(services.worker.build.dockerfilePath, services.api.build.dockerfilePath);
  assert.equal(services.bot.build.dockerfilePath, "deploy/railway/bot.Dockerfile");
});
