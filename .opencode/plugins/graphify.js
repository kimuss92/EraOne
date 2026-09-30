// graphify OpenCode plugin
// Auto-loads knowledge graph context at session start and prevents redundant file reads.
import { existsSync, readFileSync, writeFileSync, mkdirSync } from "fs";
import { join } from "path";

export default async ({ directory }) => {
  let contextLoaded = false;
  let readCache = {};
  const graphDir = join(directory, "graphify-out");
  const graphPath = join(graphDir, "graph.json");
  const cachePath = join(graphDir, ".read_cache.json");
  const summaryPath = join(graphDir, ".context_summary.json");

  // Load existing read cache if available
  if (existsSync(cachePath)) {
    try {
      readCache = JSON.parse(readFileSync(cachePath, "utf-8"));
    } catch {
      readCache = {};
    }
  }

  // Generate context summary from graph if not cached
  if (existsSync(graphPath) && !existsSync(summaryPath)) {
    try {
      const graph = JSON.parse(readFileSync(graphPath, "utf-8"));
      const communities = {};
      const godNodes = [];

      for (const [id, node] of Object.entries(graph.nodes || {})) {
        if (node.type === "community") {
          communities[id] = node.label || `Community ${id}`;
        }
        if (node.is_god_node) {
          godNodes.push(node.label || id);
        }
      }

      const summary = {
        generated: new Date().toISOString(),
        nodeCount: Object.keys(graph.nodes || {}).length,
        edgeCount: Object.keys(graph.edges || {}).length,
        communityCount: Object.keys(communities).length,
        topCommunities: Object.values(communities).slice(0, 20),
        godNodes: godNodes.slice(0, 10),
        topModules: Object.values(graph.nodes || {})
          .filter(n => n.type === "module" || n.type === "file")
          .map(n => n.label || n.id)
          .slice(0, 30),
      };

      writeFileSync(summaryPath, JSON.stringify(summary, null, 2));
    } catch {
      // Graph parse failed, continue without summary
    }
  }

  return {
    "tool.execute.before": async (input, output) => {
      // Auto-load context summary on first bash command
      if (!contextLoaded && existsSync(summaryPath)) {
        try {
          const summary = JSON.parse(readFileSync(summaryPath, "utf-8"));
          const contextMsg = [
            `[graphify] Knowledge graph loaded: ${summary.nodeCount} nodes, ${summary.edgeCount} edges, ${summary.communityCount} communities`,
            `Top modules: ${summary.topModules.slice(0, 10).join(", ")}`,
            `For questions: use \`graphify query "<question>"\` instead of reading files.`,
            `For architecture: read \`graphify-out/GRAPH_REPORT.md\``,
            summary.godNodes.length > 0 ? `Key nodes: ${summary.godNodes.slice(0, 5).join(", ")}` : null,
          ].filter(Boolean).join("\n");

          if (input.tool === "bash") {
            output.args.command = `echo "${contextMsg}" && ${output.args.command}`;
            contextLoaded = true;
          }
        } catch {
          contextLoaded = true;
        }
      }

      // Prevent redundant file reads for files in graphify-out
      if (input.tool === "read" && input.args?.filePath) {
        const filePath = input.args.filePath;
        const cacheKey = filePath.replace(directory, "").replace(/\\/g, "/");

        if (readCache[cacheKey]) {
          output.result = {
            content: readCache[cacheKey].content,
            success: true,
            cached: true,
          };
          return output;
        }
      }

      // Track file reads in bash commands
      if (input.tool === "bash" && input.args?.command) {
        const cmd = input.args.command;
        const catMatch = cmd.match(/cat\s+["']?([^\s"']+)["']?/);
        const headMatch = cmd.match(/head\s+(?:-\d+\s+)?["']?([^\s"']+)["']?/);

        if (catMatch || headMatch) {
          const file = (catMatch || headMatch)[1];
          const fullPath = file.startsWith("/") ? file : join(directory, file);
          const cacheKey = fullPath.replace(directory, "").replace(/\\/g, "/");

          if (!readCache[cacheKey] && existsSync(fullPath)) {
            try {
              const content = readFileSync(fullPath, "utf-8");
              readCache[cacheKey] = {
                content: content.slice(0, 10000),
                timestamp: new Date().toISOString(),
              };
              writeFileSync(cachePath, JSON.stringify(readCache, null, 2));
            } catch {
              // File read failed, continue
            }
          }
        }
      }
    },
  };
};
