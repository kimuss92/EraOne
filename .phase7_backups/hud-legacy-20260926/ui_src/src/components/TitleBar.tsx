import { memo } from 'react';
import { Minus, Square, Copy, X, Sun, Moon } from 'lucide-react';

interface TitleBarProps {
  maximized: boolean;
  onWindowControl: (action: string) => void;
  themeMode: 'dark' | 'light';
  onToggleTheme: () => void;
}

// (The clock/date card is gone by explicit request — the OS taskbar already
// tells the time; the rail is for Era's own state.)

function TitleBar({ maximized, onWindowControl, themeMode, onToggleTheme }: TitleBarProps) {
  return (
    <div
      className="ac-border-soft z-30 flex h-10 shrink-0 select-none items-center justify-between border-b backdrop-blur-sm"
      style={{ background: 'var(--panel-2)' }}
    >
      <div className="flex items-center gap-2 pl-4">
        <span className="ac-dot inline-block h-2 w-2 rounded-full" />
        <span className="ac-text text-[11px] font-semibold tracking-[0.25em]">
          ERA ONE — CTRLZ INTELLIGENCE SYSTEM
        </span>
      </div>
      <div className="flex">
        <button
          onClick={onToggleTheme}
          className="ac-label flex h-10 w-11 cursor-pointer items-center justify-center transition hover:bg-[var(--line-soft)]"
          title={themeMode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          aria-label={themeMode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {themeMode === 'dark' ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
        </button>
        <button
          onClick={() => onWindowControl('minimize')}
          className="ac-label flex h-10 w-11 cursor-pointer items-center justify-center transition hover:bg-[var(--line-soft)]"
          title="Minimize"
          aria-label="Minimize window"
        >
          <Minus className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => onWindowControl(maximized ? 'restore' : 'maximize')}
          className="ac-label flex h-10 w-11 cursor-pointer items-center justify-center transition hover:bg-[var(--line-soft)]"
          title={maximized ? 'Restore' : 'Maximize'}
          aria-label={maximized ? 'Restore window' : 'Maximize window'}
        >
          {maximized ? <Copy className="h-3 w-3" /> : <Square className="h-3 w-3" />}
        </button>
        <button
          onClick={() => onWindowControl('close')}
          className="ac-label flex h-10 w-11 cursor-pointer items-center justify-center transition hover:bg-red-500/10 hover:text-red-500"
          title="Close"
          aria-label="Close window"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

export default memo(TitleBar);
