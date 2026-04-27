---
version: alpha
name: Roblox Bubble
description: Kid-MMO energy: chunky 3D buttons, primary bubbles.
colors:
  primary: "#1A1A2E"
  secondary: "#6B6FA2"
  tertiary: "#00A2FF"
  neutral: "#E6F4FF"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Fredoka
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Fredoka
    fontSize: 2.4rem
    fontWeight: 700
  body:
    fontFamily: Fredoka
    fontSize: 1rem
    lineHeight: 1.55
  label:
    fontFamily: Fredoka
    fontSize: 0.8rem
    fontWeight: 700
    letterSpacing: "0.04em"
rounded:
  sm: 10px
  md: 18px
  lg: 30px
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

A kid-MMO system with chunky 3D-ish buttons and primary bubble colors.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A1A2E`):** Headlines and core text.
- **Secondary (`#6B6FA2`):** Borders, captions, and metadata.
- **Tertiary (`#00A2FF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E6F4FF`):** The page foundation.

## Typography

- **display:** Fredoka 4.5rem
- **h1:** Fredoka 2.4rem
- **body:** Fredoka 1rem
- **label:** Fredoka 0.8rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
