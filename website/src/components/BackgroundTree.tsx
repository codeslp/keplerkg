import { useEffect, useRef } from "react";

interface Node3D {
  x: number;
  y: number;
  z: number;
  r: number;
  tint: number;
  links: number[];
}

interface ProjectedNode {
  sx: number;
  sy: number;
  z: number;
  radius: number;
  tint: number;
  links: number[];
}

interface Palette {
  edge: string;
  nodes: string[];
}

const ROTATION_MS = 240_000;

function seededRand(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1103515245 + 12345) & 0x7fffffff;
    return state / 0x7fffffff;
  };
}

function sqDist(a: { x: number; y: number; z: number }, b: { x: number; y: number; z: number }): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  const dz = a.z - b.z;
  return dx * dx + dy * dy + dz * dz;
}

function rotateTilted(node: Node3D, angle: number): { x: number; y: number; z: number } {
  const tilt = (24 * Math.PI) / 180;
  const ax = Math.sin(tilt);
  const ay = Math.cos(tilt);
  const az = 0;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  const dot = node.x * ax + node.y * ay + node.z * az;

  return {
    x: node.x * cos + (ay * node.z - az * node.y) * sin + ax * dot * (1 - cos),
    y: node.y * cos + (az * node.x - ax * node.z) * sin + ay * dot * (1 - cos),
    z: node.z * cos + (ax * node.y - ay * node.x) * sin + az * dot * (1 - cos),
  };
}

function buildConstellation(seed: number): Node3D[] {
  const rand = seededRand(seed);
  const nodes: Node3D[] = [];
  const clusterOf: number[] = [];
  const outerCount = 10;
  const innerCount = 3;
  const anchors: Array<{ x: number; y: number; z: number; inner: boolean }> = [];

  for (let i = 0; i < outerCount; i += 1) {
    const theta = rand() * Math.PI * 2;
    const phi = Math.acos(2 * rand() - 1);
    const radius = 0.6 + rand() * 0.9;
    anchors.push({
      x: radius * Math.sin(phi) * Math.cos(theta),
      y: radius * Math.sin(phi) * Math.sin(theta),
      z: radius * Math.cos(phi),
      inner: false,
    });
  }

  for (let i = 0; i < innerCount; i += 1) {
    const theta = rand() * Math.PI * 2;
    const phi = Math.acos(2 * rand() - 1);
    const radius = 0.12 + rand() * 0.22;
    anchors.push({
      x: radius * Math.sin(phi) * Math.cos(theta),
      y: radius * Math.sin(phi) * Math.sin(theta),
      z: radius * Math.cos(phi),
      inner: true,
    });
  }

  const hubIndices: number[] = [];
  for (let clusterIndex = 0; clusterIndex < anchors.length; clusterIndex += 1) {
    const anchor = anchors[clusterIndex];
    const tint = clusterIndex % 3;

    hubIndices.push(nodes.length);
    nodes.push({
      x: anchor.x,
      y: anchor.y,
      z: anchor.z,
      r: anchor.inner ? 2.2 + rand() * 0.8 : 2.8 + rand() * 1.1,
      tint,
      links: [],
    });
    clusterOf.push(clusterIndex);

    const members = anchor.inner ? 7 + Math.floor(rand() * 5) : 10 + Math.floor(rand() * 7);
    for (let member = 0; member < members; member += 1) {
      const spread = anchor.inner ? 0.1 + rand() * 0.08 : 0.12 + rand() * 0.14;
      nodes.push({
        x: anchor.x + (rand() - 0.5) * spread,
        y: anchor.y + (rand() - 0.5) * spread,
        z: anchor.z + (rand() - 0.5) * spread,
        r: 1.05 + rand() * 0.95,
        tint,
        links: [],
      });
      clusterOf.push(clusterIndex);
    }
  }

  for (let i = 0; i < 40; i += 1) {
    const theta = rand() * Math.PI * 2;
    const phi = Math.acos(2 * rand() - 1);
    const radius = 0.4 + rand() * 1.2;
    nodes.push({
      x: radius * Math.sin(phi) * Math.cos(theta),
      y: radius * Math.sin(phi) * Math.sin(theta),
      z: radius * Math.cos(phi),
      r: 1.1 + rand(),
      tint: Math.floor(rand() * 3),
      links: [],
    });
    clusterOf.push(-1);
  }

  for (let index = 0; index < nodes.length; index += 1) {
    const cluster = clusterOf[index];

    if (cluster >= 0 && index !== hubIndices[cluster]) {
      nodes[index].links.push(hubIndices[cluster]);
      continue;
    }

    if (cluster >= 0 && index === hubIndices[cluster]) {
      const nearestHubs = hubIndices
        .filter((hub) => hub !== index)
        .map((hub) => ({ hub, d: sqDist(nodes[index], nodes[hub]) }))
        .sort((left, right) => left.d - right.d);

      nodes[index].links.push(nearestHubs[0].hub, nearestHubs[1].hub);
      if (nearestHubs[2] && rand() < 0.5) {
        nodes[index].links.push(nearestHubs[2].hub);
      }

      const nearestFreeNodes = nodes
        .map((_, candidate) => candidate)
        .filter((candidate) => clusterOf[candidate] === -1)
        .map((candidate) => ({ candidate, d: sqDist(nodes[index], nodes[candidate]) }))
        .sort((left, right) => left.d - right.d);

      if (nearestFreeNodes[0]) {
        nodes[index].links.push(nearestFreeNodes[0].candidate);
      }
      if (nearestFreeNodes[1]) {
        nodes[index].links.push(nearestFreeNodes[1].candidate);
      }
      continue;
    }

    const nearestNodes = nodes
      .map((_, candidate) => candidate)
      .filter((candidate) => candidate !== index)
      .map((candidate) => ({ candidate, d: sqDist(nodes[index], nodes[candidate]) }))
      .sort((left, right) => left.d - right.d);

    nodes[index].links.push(nearestNodes[0].candidate);
  }

  const seen = new Set<string>();
  const dedupedLinks = nodes.map(() => [] as number[]);
  for (let index = 0; index < nodes.length; index += 1) {
    for (const target of nodes[index].links) {
      if (index === target) {
        continue;
      }
      const low = Math.min(index, target);
      const high = Math.max(index, target);
      const key = `${low}-${high}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      dedupedLinks[low].push(high);
    }
  }

  for (let index = 0; index < nodes.length; index += 1) {
    nodes[index].links = dedupedLinks[index];
  }

  return nodes;
}

function readHslVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function withAlpha(hsl: string, alpha: number): string {
  return `hsl(${hsl} / ${alpha})`;
}

function readPalette(): Palette {
  return {
    edge: readHslVar("--graph-edge", "222 47% 25%"),
    nodes: [
      readHslVar("--graph-node-1", "263 70% 65%"),
      readHslVar("--graph-node-2", "180 100% 70%"),
      readHslVar("--graph-node-3", "142 76% 65%"),
    ],
  };
}

export default function BackgroundTree() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const nodesRef = useRef<Node3D[]>(buildConstellation(42));
  const paletteRef = useRef<Palette>({
    edge: "222 47% 25%",
    nodes: ["263 70% 65%", "180 100% 70%", "142 76% 65%"],
  });
  const startRef = useRef(0);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return undefined;
    }

    const ctx = canvas.getContext("2d");
    if (!ctx) {
      return undefined;
    }

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    paletteRef.current = readPalette();

    const draw = (now: number) => {
      if (!startRef.current) {
        startRef.current = now;
      }

      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) {
        return;
      }

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const nextWidth = Math.floor(rect.width * dpr);
      const nextHeight = Math.floor(rect.height * dpr);
      if (canvas.width !== nextWidth || canvas.height !== nextHeight) {
        canvas.width = nextWidth;
        canvas.height = nextHeight;
      }

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);

      const elapsed = now - startRef.current;
      const angle = reducedMotion.matches ? Math.PI / 10 : (elapsed / ROTATION_MS) * Math.PI * 2;
      const cx = rect.width / 2;
      const cy = rect.height / 2;
      const scale = Math.max(rect.width, rect.height) * 0.72;
      const palette = paletteRef.current;

      const projected: ProjectedNode[] = nodesRef.current.map((node) => {
        const rotated = rotateTilted(node, angle);
        const depth = (rotated.z + 1) / 2;
        const perspective = 0.58 + depth * 0.7;
        return {
          sx: cx + rotated.x * scale * perspective,
          sy: cy + rotated.y * scale * perspective,
          z: rotated.z,
          radius: node.r * (0.28 + depth * 0.28),
          tint: node.tint,
          links: node.links,
        };
      });

      ctx.lineCap = "round";
      for (let index = 0; index < projected.length; index += 1) {
        const source = projected[index];
        for (const targetIndex of source.links) {
          const target = projected[targetIndex];
          const depth = (source.z + target.z + 2) / 4;
          ctx.strokeStyle = withAlpha(palette.edge, 0.08 + depth * 0.22);
          ctx.lineWidth = 0.35 + depth * 0.65;
          ctx.beginPath();
          ctx.moveTo(source.sx, source.sy);
          ctx.lineTo(target.sx, target.sy);
          ctx.stroke();
        }
      }

      for (const node of projected) {
        const depth = (node.z + 1) / 2;
        const tint = palette.nodes[node.tint % palette.nodes.length];
        ctx.shadowBlur = 6 + depth * 8;
        ctx.shadowColor = withAlpha(tint, 0.14 + depth * 0.2);
        ctx.fillStyle = withAlpha(tint, 0.28 + depth * 0.5);
        ctx.beginPath();
        ctx.arc(node.sx, node.sy, node.radius, 0, Math.PI * 2);
        ctx.fill();

        ctx.shadowBlur = 0;
        ctx.strokeStyle = withAlpha(tint, 0.24 + depth * 0.3);
        ctx.lineWidth = 0.55;
        ctx.beginPath();
        ctx.arc(node.sx, node.sy, node.radius + 0.4, 0, Math.PI * 2);
        ctx.stroke();
      }
    };

    const renderFrame = (now: number) => {
      draw(now);
      if (!reducedMotion.matches) {
        rafRef.current = window.requestAnimationFrame(renderFrame);
      }
    };

    const restart = () => {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
      }
      startRef.current = 0;
      renderFrame(window.performance.now());
    };

    const updatePalette = () => {
      paletteRef.current = readPalette();
      draw(window.performance.now());
    };

    const observer = new MutationObserver(updatePalette);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "style"],
    });

    const handleResize = () => draw(window.performance.now());
    const handleMotionChange = () => restart();

    window.addEventListener("resize", handleResize);
    reducedMotion.addEventListener("change", handleMotionChange);
    restart();

    return () => {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
      }
      observer.disconnect();
      window.removeEventListener("resize", handleResize);
      reducedMotion.removeEventListener("change", handleMotionChange);
    };
  }, []);

  return <canvas ref={canvasRef} className="ambient-graph-bg" aria-hidden="true" />;
}
