---
name: netflix-controller
description: "Netflix automation knowledge: keyboard shortcuts, player controls, DOM structure, and best practices for programmatic control. Use when controlling Netflix playback, searching content, or interacting with the Netflix player."
allowed-tools: Read, Grep, Glob, Bash
---

# Netflix Controller Skill

## Overview

This skill provides comprehensive knowledge about Netflix automation, including keyboard shortcuts, player controls, DOM structure, and best practices for programmatic control via Playwright/CDP.

## Netflix Keyboard Shortcuts

### Playback Controls

| Key | Action |
|-----|--------|
| `Space` or `K` | Play/Pause |
| `F` | Toggle Fullscreen |
| `M` | Toggle Mute |
| `Esc` | Exit Fullscreen |
| `→` (Right Arrow) | Fast Forward 10s |
| `←` (Left Arrow) | Rewind 10s |
| `↑` (Up Arrow) | Volume Up |
| `↓` (Down Arrow) | Volume Down |
| `J` | Rewind 10s (alternative) |
| `L` | Fast Forward 10s (alternative) |

### Navigation

| Key | Action |
|-----|--------|
| `Tab` | Navigate between elements |
| `Enter` | Select/Confirm |
| `Esc` | Go back / Close modal |
| `PgUp` | Scroll up |
| `PgDn` | Scroll down |

### Subtitles & Audio

| Key | Action |
|-----|--------|
| `S` | Toggle Subtitles |
| `C` | Toggle Closed Captions |
| `Shift+Left` | Previous Subtitle |
| `Shift+Right` | Next Subtitle |

### Seeking (while paused)

| Key | Action |
|-----|--------|
| `←` | Frame back |
| `→` | Frame forward |
| `Shift+←` | Previous frame |
| `Shift+→` | Next frame |

## Netflix Player DOM Structure

### Key Selectors

```javascript
// Player container
'[data-uia="player"]'
'.nf-player-container'
'.watch-video'

// Video element
'video'

// Player controls
'[data-uia="player-controls"]'
'.PlayerControlsNeo__layout'

// Play/Pause button
'[data-uia="player-play-pause"]'
'button[data-uia="play"]'

// Progress bar
'[data-uia="progress-bar"]'
'.PlayerControlsNeo__progress-bar'

// Volume control
'[data-uia="volume"]'
'.PlayerControlsNeo__volume'

// Subtitles
'[data-uia="player-subtitle"]'
'.PlayerControlsNeo__subtitle'

// Fullscreen
'[data-uia="player-fullscreen"]'
'.PlayerControlsNeo__fullscreen'

// Cast
'[data-uia="control-cast"]'
'button[aria-label*="cast" i]'
```

### State Detection

```javascript
// Check if playing
document.querySelector('video')?.paused === false

// Check current time
document.querySelector('video')?.currentTime

// Check duration
document.querySelector('video')?.duration

// Check volume
document.querySelector('video')?.volume

// Check muted
document.querySelector('video')?.muted
```

## Netflix URL Patterns

```
https://www.netflix.com/browse          # Home/Browse
https://www.netflix.com/search?q=...    # Search results
https://www.netflix.com/title/XXXXX     # Title details
https://www.netflix.com/watch/XXXXX     # Player (active playback)
```

## Best Practices for Netflix Automation

### 1. Always Verify Playback

```python
def verify_playback(page):
    """Verify Netflix is actually playing."""
    return page.evaluate("""
        () => {
            const video = document.querySelector('video');
            return video && !video.paused && video.currentTime > 0;
        }
    """)
```

### 2. Handle Modals

Netflix uses modals for title details. Close them before navigation:

```python
def close_modal(page):
    """Close any open Netflix modal."""
    page.evaluate("""
        () => {
            const modal = document.querySelector('.modal-container, .detail-modal');
            if (modal) {
                const closeBtn = modal.querySelector('button[aria-label*="close" i], button[aria-label*="fermer" i]');
                if (closeBtn) closeBtn.click();
            }
        }
    """)
```

### 3. Use Aria Labels

Netflix uses ARIA labels extensively. Prefer them over text content:

```python
# Good - uses aria-label
page.click('button[aria-label="Play"]')

# Less reliable - depends on text content
page.click('button:has-text("Play")')
```

### 4. Handle Language Variants

Netflix supports multiple languages. Use case-insensitive matching:

```python
# Handles "Play", "Lire", "Reproduce", etc.
page.click('button[aria-label*="play" i]')
```

### 5. Wait for Player Ready

```python
def wait_for_player(page, timeout=10):
    """Wait for Netflix player to be ready."""
    page.wait_for_selector('video', timeout=timeout * 1000)
    page.wait_for_function('() => document.querySelector("video")?.readyState >= 2', timeout=timeout * 1000)
```

## Common Netflix Automation Patterns

### Search and Play

```python
async def search_and_play(page, title):
    """Search Netflix and play a title."""
    # Navigate to search
    await page.goto(f'https://www.netflix.com/search?q={title}')
    await page.wait_for_load_state('domcontentloaded')
    
    # Click first result
    await page.click(f'a[aria-label*="{title}" i]', timeout=5000)
    await page.wait_for_timeout(1500)
    
    # Click play button
    await page.click('button[aria-label*="play" i]', timeout=5000)
    
    # Verify playback
    return await verify_playback(page)
```

### Skip Intro/Recap

```python
def skip_intro(page):
    """Skip Netflix intro if present."""
    try:
        page.click('button[data-uia="skip-button"], button:has-text("Skip")', timeout=3000)
    except:
        pass  # No skip button available
```

### Toggle Subtitles

```python
def toggle_subtitles(page, enable=True):
    """Enable or disable subtitles."""
    current = page.evaluate("() => document.querySelector('video')?.textTracks[0]?.mode")
    if enable and current != 'showing':
        page.click('button[aria-label*="subtitle" i]')
    elif not enable and current == 'showing':
        page.click('button[aria-label*="subtitle" i]')
```

## Troubleshooting

### Common Issues

1. **Play button not found**: Netflix may be showing a profile selection screen. Select a profile first.

2. **Video not playing**: Check if the video element exists and has `readyState >= 2`.

3. **Modal blocking clicks**: Close any open modals before interacting with player controls.

4. **Language mismatch**: Use case-insensitive aria-label selectors to handle multiple languages.

5. **Dynamic content**: Netflix loads content dynamically. Wait for `domcontentloaded` or `networkidle`.

### Debug Commands

```python
# Get current Netflix state
state = page.evaluate("""
    () => ({
        url: window.location.href,
        title: document.title,
        hasVideo: !!document.querySelector('video'),
        isPlaying: document.querySelector('video')?.paused === false,
        currentTime: document.querySelector('video')?.currentTime,
        duration: document.querySelector('video')?.duration
    })
""")
```
