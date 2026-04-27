---
version: alpha
name: Tokyo Midnight
description: Rain-slick streets, neon reflections, focused.
colors:
  primary: "#F2F2F5"
  secondary: "#7A8699"
  tertiary: "#FF4D7E"
  neutral: "#0B0E1A"
  surface: "#141828"
  on-primary: "#0B0E1A"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.06em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
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

A cinematic dark palette. Deep blue-black surface, cold steel support, a single hot-pink accent for emphasis.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F2F2F5`):** Headlines and core text.
- **Secondary (`#7A8699`):** Borders, captions, and metadata.
- **Tertiary (`#FF4D7E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B0E1A`):** The page foundation.

## Typography

- **display:** Space Grotesk 4rem
- **h1:** Space Grotesk 2.25rem
- **body:** Inter 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
