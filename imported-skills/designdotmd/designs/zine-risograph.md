---
version: alpha
name: Zine Risograph
description: Riso-printed zines: fluoro pink on pulp paper.
colors:
  primary: "#181818"
  secondary: "#666666"
  tertiary: "#FF48B0"
  neutral: "#F0E8D6"
  surface: "#F8F1DF"
  on-primary: "#F8F1DF"
typography:
  display:
    fontFamily: Syne
    fontSize: 5rem
    fontWeight: 800
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Syne
    fontSize: 2.4rem
    fontWeight: 700
  body:
    fontFamily: Space Grotesk
    fontSize: 0.98rem
    lineHeight: 1.6
  label:
    fontFamily: Space Mono
    fontSize: 0.72rem
    letterSpacing: "0.08em"
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

A risograph-zine aesthetic: fluoro pink and teal over pulp paper, smudgy grain.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#181818`):** Headlines and core text.
- **Secondary (`#666666`):** Borders, captions, and metadata.
- **Tertiary (`#FF48B0`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0E8D6`):** The page foundation.

## Typography

- **display:** Syne 5rem
- **h1:** Syne 2.4rem
- **body:** Space Grotesk 0.98rem
- **label:** Space Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
