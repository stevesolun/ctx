---
version: alpha
name: Film Photography
description: 35mm grain: gelatin silver, kodak yellow, frame number.
colors:
  primary: "#151412"
  secondary: "#777268"
  tertiary: "#F2C94C"
  neutral: "#EEEAE0"
  surface: "#FBF7EC"
  on-primary: "#151412"
typography:
  display:
    fontFamily: Space Mono
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Space Mono
    fontSize: 1.8rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.65
  label:
    fontFamily: Space Mono
    fontSize: 0.72rem
    letterSpacing: "0.1em"
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

A film-photography palette: silver gelatin neutral, kodak-yellow accent, mono frame counters.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#151412`):** Headlines and core text.
- **Secondary (`#777268`):** Borders, captions, and metadata.
- **Tertiary (`#F2C94C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEEAE0`):** The page foundation.

## Typography

- **display:** Space Mono 3.5rem
- **h1:** Space Mono 1.8rem
- **body:** Inter 0.95rem
- **label:** Space Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
