// pi extension: the lab assistant's `plan` and `check` tools.
// plan: declare the one idea of this job, once, before editing. The worker sees the call in pi's JSON events and
//   records it in the substrate, so other assistants on the island steer away from it.
// check: build gen.py and run the circuit on a fixed sample of x (loop/check.py), inside the job's sandbox.
//   Refused until a plan is declared. The worker sets LOOP_PYTHON, LOOP_REPO and LOOP_CHECK_MAX; the call limit is
//   enforced here, per job.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
  const max = Number(process.env.LOOP_CHECK_MAX ?? "8");
  let used = 0;
  let planned = "";

  pi.registerTool({
    name: "plan",
    label: "Plan",
    description:
      "Declare the ONE concrete change this job will make, in one line, after reading the code and before your " +
      "first edit. Other assistants on the island see it and avoid duplicating it. Call it exactly once.",
    parameters: Type.Object({
      idea: Type.String({ description: "One line, e.g. 'narrow the low adder window by one bit'", maxLength: 500 }),
    }),
    async execute(_id, params) {
      if (planned) {
        return {
          content: [{ type: "text", text: `Already planned: "${planned}". Keep to that idea.` }],
          details: { planned },
          isError: true,
        };
      }
      planned = String(params.idea).trim().slice(0, 500);
      return { content: [{ type: "text", text: `Planned: ${planned}` }], details: { planned } };
    },
  });

  pi.registerTool({
    name: "check",
    label: "Check",
    description:
      "Build gen.py and test the circuit on a fixed sample of x values (edge cases plus random ones). " +
      "Reports PASS or FAIL, the first error (build traceback or the first wrong x), the qubit count, " +
      "Toffolis per shot and an estimated cost. Takes a few seconds. Limited number of calls per job.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal, _onUpdate, ctx) {
      if (!planned) {
        return {
          content: [{ type: "text", text: "Call `plan` with your one idea before running `check`." }],
          details: { used, max },
          isError: true,
        };
      }
      if (used >= max) {
        return {
          content: [{ type: "text", text: `No checks left (limit ${max} per job). Finish now: keep a passing version or revert.` }],
          details: { used, max },
          isError: true,
        };
      }
      used += 1;
      const repo = process.env.LOOP_REPO ?? "";
      const code = `import sys; sys.path.insert(0, ${JSON.stringify(repo)}); from loop.check import main; sys.exit(main())`;
      const r = await pi.exec(process.env.LOOP_PYTHON ?? "python3", ["-I", "-c", code, ctx.cwd], {
        signal,
        timeout: 300_000,
        cwd: ctx.cwd,
      });
      const out = (r.stdout || r.stderr || "check produced no output").slice(-4000).trim();
      return {
        content: [{ type: "text", text: `${out}\n(check ${used} of ${max})` }],
        details: { code: r.code, used, max },
      };
    },
  });
}
