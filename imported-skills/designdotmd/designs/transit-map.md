---
version: alpha
name: Transit Map
description: Tokyo metro: primary colored lines, precise signage.
colors:
  primary: "#121212"
  secondary: "#666666"
  tertiary: "#0064D2"
  neutral: "#F8F8F8"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Work Sans
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Work Sans
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: Work Sans
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Work Sans
    fontSize: 0.72rem
    fontWeight: 700
    letterSpacing: "0.08em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A transit-signage system: disciplined sans, route-coded primaries.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#121212`):** Headlines and core text.
- **Secondary (`#666666`):** Borders, captions, and metadata.
- **Tertiary (`#0064D2`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F8F8F8`):** The page foundation.

## Typography

- **display:** Work Sans 3.75rem
- **h1:** Work Sans 2.25rem
- **body:** Work Sans 0.95rem
- **label:** Work Sans 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
