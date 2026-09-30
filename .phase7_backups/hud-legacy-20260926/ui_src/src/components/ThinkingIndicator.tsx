import { Loader2 } from 'lucide-react';

interface ThinkingIndicatorProps {
  mode: 'thinking' | 'processing';
}

// Colors derive from the emotion accent vars (--ah/--as), so thinking shows
// violet and processing shows ember without this component knowing the theme.
const RING = 'hsl(var(--ah) var(--as) var(--al-dim) / 0.6)';
const RING_SOFT = 'hsl(var(--ah) var(--as) var(--al-dim) / 0.35)';
const GLOW = 'hsl(var(--ah) var(--as) 50% / var(--glow-a))';

export default function ThinkingIndicator({ mode }: ThinkingIndicatorProps) {
  if (mode === 'processing') {
    return (
      <div className="absolute inset-0 z-20 flex items-center justify-center pointer-events-none">
        <div className="relative flex items-center justify-center">
          {/* Outer ring */}
          <div
            className="absolute h-20 w-20 rounded-full border-2"
            style={{
              borderColor: RING,
              animation: 'processing-ring 2s ease-in-out infinite',
              boxShadow: `0 0 30px ${GLOW}, 0 0 60px ${GLOW}`,
            }}
          />
          {/* Inner ring */}
          <div
            className="absolute h-14 w-14 rounded-full border"
            style={{
              borderColor: RING_SOFT,
              animation: 'processing-ring 2s ease-in-out infinite 0.4s',
              boxShadow: `0 0 20px ${GLOW}`,
            }}
          />
          {/* Center icon */}
          <Loader2
            className="h-6 w-6"
            style={{ color: 'hsl(var(--ah) var(--as) var(--al-text))', animation: 'spin 1.5s linear infinite' }}
          />
        </div>
      </div>
    );
  }

  // Thinking: pulsing dots
  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center pointer-events-none">
      <div className="flex items-center gap-2">
        {[0, 1, 2].map(i => (
          <div
            key={i}
            className="h-3 w-3 rounded-full"
            style={{
              backgroundColor: 'hsl(var(--ah) var(--as) var(--al-fill))',
              animation: `thinking-dot 1.4s ease-in-out infinite ${i * 0.2}s`,
              boxShadow: `0 0 12px ${GLOW}`,
            }}
          />
        ))}
      </div>
    </div>
  );
}
