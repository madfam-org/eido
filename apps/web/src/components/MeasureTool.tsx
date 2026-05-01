"use client";

import { useRef, useState, useCallback } from "react";
import { useThree } from "@react-three/fiber";
import { Line, Html } from "@react-three/drei";
import * as THREE from "three";

interface MeasurementPoint {
  id: string;
  position: THREE.Vector3;
}

interface Measurement {
  id: string;
  a: THREE.Vector3;
  b: THREE.Vector3;
  distanceMm: number;
}

interface MeasureToolProps {
  scaleMetric?: string;   // "millimeters" | "centimeters" | "meters"
  enabled: boolean;
  onAnnotate?: (x: number, y: number, z: number) => void;
}

// Conversion factors to mm
const TO_MM: Record<string, number> = {
  millimeters: 1,
  centimeters: 10,
  meters: 1000,
};

function formatDistance(mm: number): string {
  if (mm >= 1000) return `${(mm / 1000).toFixed(2)} m`;
  if (mm >= 10) return `${(mm / 10).toFixed(1)} cm`;
  return `${mm.toFixed(1)} mm`;
}

/**
 * MeasureTool — click two points on the 3D surface to measure distance.
 * Uses raycasting from R3F camera. Works with any Three.js mesh in the scene.
 */
export function MeasureTool({ scaleMetric = "millimeters", enabled, onAnnotate }: MeasureToolProps) {
  const { camera, gl, scene } = useThree();
  const [points, setPoints] = useState<MeasurementPoint[]>([]);
  const [measurements, setMeasurements] = useState<Measurement[]>([]);
  const raycaster = useRef(new THREE.Raycaster());

  const handleClick = useCallback((event: MouseEvent) => {
    if (!enabled) return;
    const canvas = gl.domElement;
    const rect = canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    );
    raycaster.current.setFromCamera(ndc, camera);
    const meshes: THREE.Object3D[] = [];
    scene.traverse((obj) => { if ((obj as THREE.Mesh).isMesh) meshes.push(obj); });
    const hits = raycaster.current.intersectObjects(meshes, true);
    if (!hits.length) return;

    const hit = hits[0].point.clone();
    const newPoint: MeasurementPoint = { id: crypto.randomUUID(), position: hit };

    setPoints((prev) => {
      if (prev.length === 0) return [newPoint];
      if (prev.length === 1) {
        const scale = TO_MM[scaleMetric] ?? 1;
        const dist = prev[0].position.distanceTo(hit) * scale;
        setMeasurements((m) => [
          ...m,
          { id: crypto.randomUUID(), a: prev[0].position, b: hit, distanceMm: dist },
        ]);
        return [];  // Reset for next pair
      }
      return [newPoint];
    });
  }, [enabled, camera, gl, scene, scaleMetric]);

  // Attach/detach click handler based on enabled state
  useCallback(() => {
    const canvas = gl.domElement;
    if (enabled) canvas.addEventListener("click", handleClick);
    return () => canvas.removeEventListener("click", handleClick);
  }, [enabled, handleClick, gl])();

  return (
    <>
      {/* Pending first point indicator */}
      {points.map((p) => (
        <mesh key={p.id} position={p.position}>
          <sphereGeometry args={[0.015, 12, 12]} />
          <meshStandardMaterial color="#f59e0b" emissive="#f59e0b" emissiveIntensity={0.5} />
        </mesh>
      ))}

      {/* Completed measurements */}
      {measurements.map((m) => (
        <group key={m.id}>
          <Line
            points={[m.a.toArray(), m.b.toArray()]}
            color="#f59e0b"
            lineWidth={2}
            dashed={false}
          />
          {/* Endpoint spheres */}
          {[m.a, m.b].map((pt, i) => (
            <mesh key={i} position={pt}>
              <sphereGeometry args={[0.012, 12, 12]} />
              <meshStandardMaterial color="#f59e0b" emissive="#f59e0b" emissiveIntensity={0.3} />
            </mesh>
          ))}
          {/* Distance label at midpoint */}
          <Html
            position={new THREE.Vector3().addVectors(m.a, m.b).multiplyScalar(0.5).toArray()}
            center
            distanceFactor={8}
          >
            <div className="bg-amber-500 text-black text-[11px] font-bold px-2 py-0.5 rounded-full shadow-lg whitespace-nowrap pointer-events-none">
              {formatDistance(m.distanceMm)}
            </div>
          </Html>
        </group>
      ))}
    </>
  );
}

/** Measurement toolbar overlay — toggle measure mode and clear measurements */
export function MeasureToolbar({
  enabled,
  onToggle,
  onClear,
}: {
  enabled: boolean;
  onToggle: () => void;
  onClear: () => void;
}) {
  return (
    <div className="absolute top-4 left-4 flex gap-2 z-10">
      <button
        onClick={onToggle}
        className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${
          enabled
            ? "bg-amber-500 text-black"
            : "glass text-slate-300 hover:text-white"
        }`}
      >
        📏 {enabled ? "Measuring…" : "Measure"}
      </button>
      {enabled && (
        <button
          onClick={onClear}
          className="px-3 py-1.5 text-xs rounded-lg glass text-slate-400 hover:text-white transition-colors"
        >
          Clear
        </button>
      )}
    </div>
  );
}
