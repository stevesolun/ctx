---
version: alpha
name: Neon Arcade
description: Synthwave violet and hot magenta.
colors:
  primary: "#F6EEFF"
  secondary: "#8B7AA5"
  tertiary: "#FF3DCA"
  neutral: "#140A1F"
  surface: "#1E1330"
  on-primary: "#140A1F"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 4.25rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: Space Grotesk
    fontSize: 1rem
    lineHeight: 1.55
  label:
    fontFamily: Space Mono
    fontSize: 0.75rem
    letterSpacing: "0.06em"
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

A night-mode palette with CRT heritage. Deep violet surfaces, magenta primary, phosphor cyan for data.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F6EEFF`):** Headlines and core text.
- **Secondary (`#8B7AA5`):** Borders, captions, and metadata.
- **Tertiary (`#FF3DCA`):** The sole driver for interaction. Reserve it.
- **Neutral (`#140A1F`):** The page foundation.

## Typography

- **display:** Space Grotesk 4.25rem
- **h1:** Space Grotesk 2.25rem
- **body:** Space Grotesk 1rem
- **label:** Space Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
