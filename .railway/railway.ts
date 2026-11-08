import { defineRailway, github, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "m100";
export default defineRailway(() => {
  const m100 = service("m100", {
    source: github("VladTichonenko/m100", { branch: "main", checkSuites: true }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/healthz",
    healthcheckTimeout: 180,
    replicas: { "iad": 1 },
    deploy: {
      drainingSeconds: 30, overlapSeconds: 0, sleepApplication: false,
      restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10,
    },
  });

  return project("M100", {
    variables: { managed: false },
    resources: [m100],
  });
});
