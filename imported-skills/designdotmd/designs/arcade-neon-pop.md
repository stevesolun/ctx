---
version: alpha
name: Arcade Neon Pop
description: Chunky buttons, rainbow XP bars, candy physics.
colors:
  primary: "#1A0B3C"
  secondary: "#8A7CA8"
  tertiary: "#FF3DA5"
  neutral: "#FFF0F6"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Luckiest Guy
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "0.02em"
  h1:
    fontFamily: Fredoka
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: Fredoka
    fontSize: 1rem
    lineHeight: 1.5
  label:
    fontFamily: Fredoka
    fontSize: 0.78rem
    fontWeight: 600
    letterSpacing: "0.04em"
rounded:
  sm: 10px
  md: 18px
  lg: 28px
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

A maximalist mobile-game system: thick outlines, saturated gradients, squishy radii.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A0B3C`):** Headlines and core text.
- **Secondary (`#8A7CA8`):** Borders, captions, and metadata.
- **Tertiary (`#FF3DA5`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FFF0F6`):** The page foundation.

## Typography

- **display:** Luckiest Guy 4.5rem
- **h1:** Fredoka 2.25rem
- **body:** Fredoka 1rem
- **label:** Fredoka 0.78rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
