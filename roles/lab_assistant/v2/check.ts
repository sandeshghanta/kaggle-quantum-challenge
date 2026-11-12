// pi extension: the lab assistant's `check` tool.
// Builds gen.py and runs the circuit on a fixed sample of x (loop/check.py), inside the job's sandbox.
// The worker sets LOOP_PYTHON, LOOP_REPO and LOOP_CHECK_MAX; the call limit is enforced here, per job.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
  const max = Number(process.env.LOOP_CHECK_MAX ?? "8");
  let used = 0;

  pi.registerTool({
    name: "check",
    label: "Check",
    description:
      "Build gen.py and test the circuit on a fixed sample of x values (edge cases plus random ones). " +
      "Reports PASS or FAIL, the first error (build traceback or the first wrong x), the qubit count, " +
      "Toffolis per shot and an estimated cost. Takes a few seconds. Limited number of calls per job.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal, _onUpdate, ctx) {
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
