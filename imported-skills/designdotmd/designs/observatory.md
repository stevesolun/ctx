---
version: alpha
name: Observatory
description: Deep-sky catalog: nebula black, star cream, redshift.
colors:
  primary: "#E9E3CD"
  secondary: "#8D8570"
  tertiary: "#E2563C"
  neutral: "#06070C"
  surface: "#0D0F17"
  on-primary: "#E9E3CD"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 5rem
    fontWeight: 500
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: IBM Plex Sans
    fontSize: 0.98rem
    lineHeight: 1.7
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.72rem
    letterSpacing: "0.12em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

An astronomy-catalog palette: deep-space black, starlight cream, redshift accent for anomalies.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E9E3CD`):** Headlines and core text.
- **Secondary (`#8D8570`):** Borders, captions, and metadata.
- **Tertiary (`#E2563C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#06070C`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 5rem
- **h1:** Cormorant Garamond 2.5rem
- **body:** IBM Plex Sans 0.98rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
