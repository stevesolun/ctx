---
version: alpha
name: Climate Atlas
description: Climate data: chart green, heatmap amber, atlas paper.
colors:
  primary: "#142018"
  secondary: "#627266"
  tertiary: "#E09A3E"
  neutral: "#F0EADC"
  surface: "#F9F4E6"
  on-primary: "#F9F4E6"
typography:
  display:
    fontFamily: Work Sans
    fontSize: 3.75rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Work Sans
    fontSize: 2rem
    fontWeight: 600
  body:
    fontFamily: Work Sans
    fontSize: 0.98rem
    lineHeight: 1.6
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.72rem
    letterSpacing: "0.08em"
rounded:
  sm: 3px
  md: 6px
  lg: 10px
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

A climate-dashboard palette: atlas paper surface, chart greens, amber heat anomalies.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#142018`):** Headlines and core text.
- **Secondary (`#627266`):** Borders, captions, and metadata.
- **Tertiary (`#E09A3E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0EADC`):** The page foundation.

## Typography

- **display:** Work Sans 3.75rem
- **h1:** Work Sans 2rem
- **body:** Work Sans 0.98rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
