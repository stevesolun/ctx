---
version: alpha
name: Comic Ink
description: Comic panel: pulp yellow, hero red, ink-black panel rules.
colors:
  primary: "#140E0A"
  secondary: "#8E7A58"
  tertiary: "#E8242C"
  neutral: "#F3D766"
  surface: "#FBE580"
  on-primary: "#140E0A"
typography:
  display:
    fontFamily: Bangers
    fontSize: 5.5rem
    fontWeight: 400
    letterSpacing: "0.02em"
  h1:
    fontFamily: Bangers
    fontSize: 2.8rem
    fontWeight: 400
  body:
    fontFamily: DM Sans
    fontSize: 0.98rem
    lineHeight: 1.55
  label:
    fontFamily: Bangers
    fontSize: 0.92rem
    letterSpacing: "0.06em"
rounded:
  sm: 0px
  md: 2px
  lg: 4px
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

A comic-book palette: pulp-paper yellow, hero red, thick ink-black panel rules.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#140E0A`):** Headlines and core text.
- **Secondary (`#8E7A58`):** Borders, captions, and metadata.
- **Tertiary (`#E8242C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3D766`):** The page foundation.

## Typography

- **display:** Bangers 5.5rem
- **h1:** Bangers 2.8rem
- **body:** DM Sans 0.98rem
- **label:** Bangers 0.92rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
