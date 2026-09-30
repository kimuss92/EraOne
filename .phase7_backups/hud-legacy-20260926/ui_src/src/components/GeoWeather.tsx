import { memo, useEffect, useState } from 'react';
import { Droplets, Wind } from 'lucide-react';

// GEO // WEATHER — faceted Tunisia map with a selectable city marker and a
// live Open-Meteo readout (free, no API key). Cost profile: one small JSON
// fetch every 10 minutes + a single 2.4s SVG ping animation (transform/opacity
// only). Everything else is static.

const CITY_KEY = 'era_geo_city';
const REFRESH_MS = 600_000; // 10 min — weather doesn't move faster than this

interface City {
  name: string;
  lat: number;
  lon: number;
  // Precomputed map-space position: x=(lon−7.5)/4.1×100, y=(37.5−lat)/7.3×160
  x: number;
  y: number;
}

const CITIES: City[] = [
  { name: 'TUNIS', lat: 36.8, lon: 10.18, x: 65.4, y: 15.3 },
  { name: 'BIZERTE', lat: 37.27, lon: 9.87, x: 57.8, y: 7.0 },
  { name: 'SOUSSE', lat: 35.83, lon: 10.64, x: 76.6, y: 36.6 },
  { name: 'KAIROUAN', lat: 35.68, lon: 10.1, x: 63.4, y: 39.9 },
  { name: 'SFAX', lat: 34.74, lon: 10.76, x: 79.5, y: 60.5 },
  { name: 'DJERBA', lat: 33.88, lon: 10.86, x: 82.0, y: 79.3 },
  { name: 'TOZEUR', lat: 33.92, lon: 8.13, x: 15.4, y: 78.5 },
];

// Low-poly Tunisia silhouette, matching the faceted-crystal look of the core.
const TUNISIA_PATH =
  'M27,12 L44,7 L58,5 L68,13 L87,10 L76,24 L77,37 L88,44 L80,61 L63,79 L82,81 L90,95 L66,149 L49,159 L38,138 L17,114 L20,66 L17,22 Z';

// WMO weather interpretation codes → short HUD labels
function wmoLabel(code: number): string {
  if (code === 0) return 'CLEAR';
  if (code <= 2) return 'PARTLY CLOUDY';
  if (code === 3) return 'OVERCAST';
  if (code === 45 || code === 48) return 'FOG';
  if (code >= 51 && code <= 57) return 'DRIZZLE';
  if (code >= 61 && code <= 67) return 'RAIN';
  if (code >= 71 && code <= 77) return 'SNOW';
  if (code >= 80 && code <= 82) return 'SHOWERS';
  if (code >= 95) return 'STORM';
  return '—';
}

interface Weather {
  temp: number;
  humidity: number;
  wind: number;
  code: number;
}

function loadCity(): string {
  const saved = localStorage.getItem(CITY_KEY);
  return CITIES.some(c => c.name === saved) ? (saved as string) : 'TUNIS';
}

function GeoWeather() {
  const [cityName, setCityName] = useState(loadCity);
  const [weather, setWeather] = useState<Weather | null>(null);
  const [failed, setFailed] = useState(false);

  const city = CITIES.find(c => c.name === cityName) ?? CITIES[0];

  useEffect(() => {
    const ctrl = new AbortController();
    const url =
      `https://api.open-meteo.com/v1/forecast?latitude=${city.lat}&longitude=${city.lon}` +
      '&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m&timezone=auto';

    const fetchWeather = () => {
      fetch(url, { signal: ctrl.signal })
        .then(r => r.json())
        .then(data => {
          const c = data?.current;
          if (!c) throw new Error('no current block');
          setWeather({
            temp: Math.round(c.temperature_2m),
            humidity: Math.round(c.relative_humidity_2m),
            wind: Math.round(c.wind_speed_10m),
            code: c.weather_code,
          });
          setFailed(false);
        })
        .catch(err => {
          if (err?.name !== 'AbortError') setFailed(true);
        });
    };

    fetchWeather();
    const t = setInterval(fetchWeather, REFRESH_MS);
    return () => {
      ctrl.abort();
      clearInterval(t);
    };
  }, [city.lat, city.lon]);

  const selectCity = (name: string) => {
    setCityName(name);
    try { localStorage.setItem(CITY_KEY, name); } catch {}
  };

  // FLAT section inside the unified left glass panel (App.tsx): its own
  // "Weather | CITY" header row (reference design), then map + readout.
  return (
    <div className="border-t border-line-soft px-4 pb-4 pt-3">
      <div className="mb-2.5 flex items-baseline justify-between">
        <span className="flex items-baseline gap-2">
          <span className="ac-text text-[12px] font-semibold tracking-wide">Weather</span>
          <span className="t3 text-[11px]">|</span>
          <select
            value={cityName}
            onChange={e => selectCity(e.target.value)}
            className="t2 cursor-pointer rounded bg-transparent text-[11px] font-medium focus:outline-none"
            title="Select location"
          >
            {CITIES.map(c => (
              <option key={c.name} value={c.name} style={{ background: 'var(--bg)', color: 'var(--text-1)' }}>
                {c.name}
              </option>
            ))}
          </select>
        </span>
      </div>

      <div className="flex items-center gap-3">
        <svg viewBox="0 0 105 165" className="h-24 w-auto shrink-0" aria-label="Tunisia map">
          <path
            d={TUNISIA_PATH}
            fill="hsl(var(--ah) var(--as) 55% / 0.07)"
            stroke="hsl(var(--ah) var(--as) 62% / 0.55)"
            strokeWidth="1.2"
            strokeLinejoin="round"
            style={{ transition: 'stroke 0.9s ease, fill 0.9s ease' }}
          />
          {/* Facet lines — echo the crystalline core aesthetic */}
          <g stroke="hsl(var(--ah) var(--as) 62% / 0.16)" strokeWidth="0.6" style={{ transition: 'stroke 0.9s ease' }}>
            <path d="M44,7 L77,37 M68,13 L20,66 M17,22 L77,37 M20,66 L80,61 M38,138 L63,79 M66,149 L17,114" fill="none" />
          </g>
          {/* Unselected cities */}
          {CITIES.filter(c => c.name !== city.name).map(c => (
            <circle key={c.name} cx={c.x} cy={c.y} r="1.8" fill="hsl(var(--ah) var(--as) 62% / 0.4)" />
          ))}
          {/* Selected city: solid marker + expanding ping */}
          <circle className="geo-ping" cx={city.x} cy={city.y} r="4" fill="none" stroke="hsl(var(--ah) var(--as) 66% / 0.8)" strokeWidth="1" />
          <circle cx={city.x} cy={city.y} r="2.6" fill="hsl(var(--ah) calc(var(--as) + 15%) 68%)" style={{ transition: 'fill 0.9s ease' }} />
        </svg>

        <div className="min-w-0 flex-1 space-y-1">
          {weather ? (
            <>
              <div className="t1 text-[26px] font-semibold leading-none tabular-nums tracking-tight">
                {weather.temp}°C
              </div>
              <div className="t2 text-[11px] font-medium capitalize">{wmoLabel(weather.code).toLowerCase()}</div>
              <div className="t3 flex items-center gap-1 text-[10px]">
                <Droplets className="h-3 w-3" /> Humidity {weather.humidity}%
              </div>
              <div className="t3 flex items-center gap-1 text-[10px]">
                <Wind className="h-3 w-3" /> Wind {weather.wind} km/h
              </div>
            </>
          ) : (
            <div className="t3 text-[11px]">{failed ? 'Link lost' : 'Syncing…'}</div>
          )}
        </div>
      </div>
    </div>
  );
}

// Memoized + zero props: App re-renders never touch this subtree.
export default memo(GeoWeather);
