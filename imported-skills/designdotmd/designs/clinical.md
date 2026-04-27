---
version: alpha
name: Clinical
description: Hospital-grade legibility, but warm.
colors:
  primary: "#0F2A3B"
  secondary: "#4F6B7C"
  tertiary: "#0E9F8E"
  neutral: "#F1F5F7"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: IBM Plex Sans
    fontSize: 3.5rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  h1:
    fontFamily: IBM Plex Sans
    fontSize: 2rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Sans
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 4px
  md: 8px
  lg: 12px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

Designed for healthcare and data-heavy interfaces. Neutral greys, thoughtful teal accent, extreme clarity at all sizes.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F2A3B`):** Headlines and core text.
- **Secondary (`#4F6B7C`):** Borders, captions, and metadata.
- **Tertiary (`#0E9F8E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F1F5F7`):** The page foundation.

## Typography

- **display:** IBM Plex Sans 3.5rem
- **h1:** IBM Plex Sans 2rem
- **body:** IBM Plex Sans 0.95rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
